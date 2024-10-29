from typing import Any, ClassVar, Literal, Optional, Union

import pulumi
import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
from pydantic import (
    BaseModel,
    ConfigDict,
    PositiveInt,
    field_validator,
    model_validator,
)

from ol_infrastructure.lib.aws.iam_helper import oidc_trust_policy_template
from ol_infrastructure.lib.ol_types import AWSBase


class OLEKSGatewayRouteConfig(BaseModel):
    backend_service_name: Optional[str]
    backend_service_namespace: Optional[str]
    backend_service_port: Optional[int]
    filters: Optional[list[dict[str, Any]]] = []
    matches: Optional[list[dict[str, Any]]] = None
    listener_name: str
    hostnames: list[str]
    name: str
    port: int


class OLEKSGatewayListenerConfig(BaseModel):
    name: str
    hostname: str
    port: int
    protocol: Literal["HTTPS", "HTTP"] = "HTTPS"
    tls_mode: Literal["Passthrough", "Terminate"] = "Terminate"
    certificate_secret_name: Optional[str]
    certificate_secret_namespace: Optional[str] = ""

    @model_validator(mode="after")
    def check_tls_config(self):
        if self.protocol == "HTTPS" and (
            not self.certificate_secret_name or not self.tls_mode
        ):
            msg = "If protocol is HTTPS then certificate_secret_name, "
            "certificate_secret_namespace, and tls_mode must be set."
            raise ValueError(msg)
        return self


class OLEKSGatewayConfig(BaseModel):
    annotations: Optional[dict[str, str]] = None
    cert_issuer: Optional[str] = None
    cert_issuer_class: Optional[Literal["cluster-issuer", "issuer"]] = "cluster-issuer"
    gateway_class_name: str = "traefik"
    gateway_name: str
    hostnames: list[str]
    http_redirect: bool = True
    labels: Optional[dict[str, str]] = {}
    namespace: str
    listeners: list[OLEKSGatewayListenerConfig] = []
    routes: list[OLEKSGatewayRouteConfig] = []

    @field_validator("gateway_class_name")
    @classmethod
    def check_gateway_class(cls, gateway_class_name: str) -> str:
        if gateway_class_name != "traefik":
            msg = "Only the 'traefik' gateway class is supported at this time."
            raise ValueError(msg)
        return gateway_class_name

    @field_validator("routes")
    @classmethod
    def check_routes(
        cls, routes: list[OLEKSGatewayRouteConfig]
    ) -> list[OLEKSGatewayRouteConfig]:
        if not routes:
            msg = "At least one route must be supplied."
            raise ValueError(msg)
        return routes

    @field_validator("listeners")
    @classmethod
    def check_listeners(
        cls, listeners: list[OLEKSGatewayListenerConfig]
    ) -> list[OLEKSGatewayListenerConfig]:
        if not listeners:
            msg = "At least one listener must be supplied."
            raise ValueError(msg)
        return listeners

    # TODO @Ardiea: create validator that ensures  # noqa: TD003, FIX002
    # each hostname supplied in routes
    # exists in the gateway hostnames list


class OLEKSGateway(pulumi.ComponentResource):
    gateway: kubernetes.apiextensions.CustomResource = None
    routes: ClassVar[list[kubernetes.apiextensions.CustomResource]] = []

    def __init__(
        self,
        name: str,
        gateway_config: OLEKSGatewayConfig,
        opts: Optional[pulumi.ResourceOptions] = None,
    ):
        super().__init__(
            "ol:infrastructure:aws:eks:OLEKSGateway",
            name,
            None,
            opts,
        )

        if gateway_config.cert_issuer:
            if gateway_config.annotations is None:
                gateway_config.annotations = {}
            gateway_config.annotations[
                f"cert-manager.io/{gateway_config.cert_issuer_class}"
            ] = gateway_config.cert_issuer

        # Create a generic HTTP route that always redirects to HTTPS
        if gateway_config.http_redirect:
            hostname_set = set()
            for route_config in gateway_config.routes:
                for hostname in route_config.hostnames:
                    hostname_set.add(hostname)

            kubernetes.apiextensions.CustomResource(
                f"{gateway_config.gateway_name}-http-redirect-httproute-resource",
                api_version="gateway.networking.k8s.io/v1",
                kind="HTTPRoute",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name=f"{gateway_config.gateway_name}-http-redirect",
                    labels=gateway_config.labels,
                    annotations=gateway_config.annotations,
                    namespace=gateway_config.namespace,
                ),
                spec={
                    "parentRefs": [
                        {
                            "name": gateway_config.gateway_name,
                            "sectionName": "http",
                            "kind": "Gateway",
                            "group": "gateway.networking.k8s.io",
                            "port": 8000,
                        },
                    ],
                    "hostnames": sorted([*hostname_set]),  # sorted to avoid stack churn
                    "rules": [
                        {
                            "filters": [
                                {
                                    "type": "RequestRedirect",
                                    "requestRedirect": {
                                        "scheme": "https",
                                    },
                                },
                            ],
                        },
                    ],
                },
                opts=pulumi.ResourceOptions(parent=self).merge(opts),
            )

        for route_config in gateway_config.routes:
            https_route_spec = {
                "parentRefs": [
                    {
                        "name": gateway_config.gateway_name,
                        "sectionName": route_config.listener_name,
                        "kind": "Gateway",
                        "group": "gateway.networking.k8s.io",
                        "port": route_config.port,
                    },
                ],
                "hostnames": route_config.hostnames,
                "rules": [
                    {
                        "backendRefs": [
                            {
                                "name": route_config.backend_service_name,
                                "namespace": route_config.backend_service_namespace,
                                "kind": "Service",
                                "port": route_config.backend_service_port,
                            }
                        ],
                        "filters": route_config.filters,
                        "matches": route_config.matches,
                    }
                ],
            }

            route_resource = kubernetes.apiextensions.CustomResource(
                f"{gateway_config.gateway_name}-{route_config.name}-httproute-resource",
                api_version="gateway.networking.k8s.io/v1",
                kind="HTTPRoute",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name=route_config.name,
                    labels=gateway_config.labels,
                    annotations=gateway_config.annotations,
                    namespace=gateway_config.namespace,
                ),
                spec=https_route_spec,
                opts=pulumi.ResourceOptions(parent=self).merge(opts),
            )
            self.routes.append(route_resource)

        listeners = []
        for listener_config in gateway_config.listeners:
            listener = {
                "name": listener_config.name,
                "allowedRoutes": {
                    "namespaces": {
                        "from": "Same",
                    }
                },
                "hostname": listener_config.hostname,
                "port": listener_config.port,
                "protocol": listener_config.protocol,
            }
            if listener_config.protocol == "HTTPS":
                listener["tls"] = {
                    "mode": listener_config.tls_mode,
                    "certificateRefs": [
                        {
                            "group": "",
                            "kind": "Secret",
                            "name": listener_config.certificate_secret_name,
                            "namespace": listener_config.certificate_secret_namespace,
                        },
                    ],
                }
            listeners.append(listener)

        self.gateway = kubernetes.apiextensions.CustomResource(
            f"{gateway_config.gateway_name}-{gateway_config.gateway_class_name}-gateway-resource",
            api_version="gateway.networking.k8s.io/v1",
            kind="Gateway",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name=gateway_config.gateway_name,
                labels=gateway_config.labels,
                annotations=gateway_config.annotations,
                namespace=gateway_config.namespace,
            ),
            spec={
                "gatewayClassName": gateway_config.gateway_class_name,
                "listeners": listeners,
            },
            opts=pulumi.ResourceOptions(parent=self).merge(opts),
        )


class OLEKSTrustRoleConfig(AWSBase):
    account_id: Union[str, PositiveInt]
    cluster_name: str
    cluster_identities: pulumi.Output
    description: str
    policy_operator: Literal["StringEquals", "StringLike"]
    role_name: str
    service_account_identifier: str
    model_config = ConfigDict(arbitrary_types_allowed=True)


class OLEKSTrustRole(pulumi.ComponentResource):
    """Component resource to create an IAM Trust Role that can be associated with a K8S
    service account
    """

    role: aws.iam.Role = None

    def __init__(
        self,
        name: str,
        role_config: OLEKSTrustRoleConfig,
        opts: Optional[pulumi.ResourceOptions] = None,
    ):
        super().__init__(
            "ol:infrastructure:aws:eks:OLEKSTrustRole",
            name,
            None,
            opts,
        )

        self.role = aws.iam.Role(
            f"{role_config.cluster_name}-{role_config.role_name}-trust-role",
            name=f"{role_config.cluster_name}-{role_config.role_name}-trust-role",
            path=f"/ol-infrastructure/eks/{role_config.cluster_name}/",
            assume_role_policy=role_config.cluster_identities.apply(
                lambda ids: oidc_trust_policy_template(
                    oidc_identifier=ids[0]["oidcs"][0]["issuer"],
                    account_id=str(role_config.account_id),
                    k8s_service_account_identifier=role_config.service_account_identifier,
                    operator=role_config.policy_operator,
                )
            ),
            description=role_config.description,
            tags=role_config.tags,
            opts=pulumi.ResourceOptions(parent=self).merge(opts),
        )
