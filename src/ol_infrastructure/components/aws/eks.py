from typing import ClassVar, Literal, Optional, Union

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
    certificate_secret_name: Optional[str]
    certificate_secret_namespace: Optional[str] = ""
    hostname: str
    name: str
    port: int
    protocol: str
    tls_mode: Literal["Passthrough", "Terminate"] = "Terminate"

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
    annotations: dict[str, str] = {}
    cert_issuer: Optional[str] = None
    cert_issuer_class: Literal["cluster-issuer", "issuer"] = "cluster-issuer"
    gateway_class_name: str = "traefik"
    gateway_name: str
    hostnames: Optional[list[str]] = []
    http_redirect: bool = True
    labels: Optional[dict[str, str]] = {}
    namespace: str
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
    def check_listeners(
        cls, routes: list[OLEKSGatewayRouteConfig]
    ) -> list[OLEKSGatewayRouteConfig]:
        if not routes:
            msg = "At least one route must be supplied."
            raise ValueError(msg)
        return routes


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

        if gateway_config.http_redirect:
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
                            "section_name": "http",
                            "kind": "Gateway",
                            "group": "gateway.networking.k8s.io",
                            "port": 8000,
                        },
                    ],
                    "hostnames": gateway_config.hostnames,
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

        listeners = []
        for route_config in gateway_config.routes:
            if not route_config.certificate_secret_namespace:
                route_config.certificate_secret_namespace = gateway_config.namespace
            listener = {
                "name": route_config.name,
                "hostname": route_config.hostname,
                "port": route_config.port,
                "protocol": route_config.protocol,
                "tls": {
                    "mode": route_config.tls_mode,
                    "certificateRefs": [
                        {
                            "group": "",
                            "kind": "Secret",
                            "name": route_config.certificate_secret_name,
                            "namespace": route_config.certificate_secret_namespace,
                        },
                    ],
                },
            }
            listeners.append(listener)

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
                spec={
                    "parentRefs": [
                        {
                            "name": gateway_config.gateway_name,
                            "section_name": route_config.name,
                            "kind": "Gateway",
                            "group": "gateway.networking.k8s.io",
                            "port": route_config.port,
                        },
                    ],
                    "hostnames": gateway_config.hostnames,
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
                        }
                    ],
                },
                opts=pulumi.ResourceOptions(parent=self).merge(opts),
            )
            self.routes.append(route_resource)

        if gateway_config.cert_issuer:
            gateway_config.annotations[
                f"cert-manager.io/{gateway_config.cert_issuer_class}"
            ] = gateway_config.cert_issuer

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
