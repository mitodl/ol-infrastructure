from typing import Any, ClassVar, Literal

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
    backend_service_name: str | None
    backend_service_namespace: str | None
    backend_service_port: int | None
    filters: list[dict[str, Any]] | None = []
    matches: list[dict[str, Any]] | None = None
    listener_name: str
    hostnames: list[str]
    name: str
    port: int

    @field_validator("hostnames")
    @classmethod
    def de_dupe_hostnames(cls, hostnames: list[str]) -> list[str]:
        # Just to be safe
        return list(set(hostnames))


class OLEKSGatewayListenerConfig(BaseModel):
    name: str
    hostname: str
    port: int
    protocol: Literal["HTTPS", "HTTP"] = "HTTPS"
    tls_mode: Literal["Passthrough", "Terminate"] = "Terminate"
    certificate_secret_name: str | None
    certificate_secret_namespace: str | None = ""

    @model_validator(mode="after")
    def check_tls_config(self):
        if self.protocol == "HTTPS" and (
            not self.certificate_secret_name
            or not self.tls_mode
            or not self.certificate_secret_namespace
        ):
            msg = "If protocol is HTTPS then certificate_secret_name, "
            "certificate_secret_namespace, and tls_mode must be supplied."
            raise ValueError(msg)
        return self


class OLEKSGatewayConfig(BaseModel):
    annotations: dict[str, str] | None = None
    cert_issuer: str | None = None
    cert_issuer_class: Literal["cluster-issuer", "issuer", "external"] | None = (
        "cluster-issuer"
    )
    gateway_class_name: str = "traefik"
    gateway_name: str
    http_redirect: bool = True
    labels: dict[str, str] | None = None
    namespace: str
    listeners: list[OLEKSGatewayListenerConfig] = []
    routes: list[OLEKSGatewayRouteConfig] = []

    @model_validator(mode="after")
    def check_cert_issuer(self):
        if self.cert_issuer_class == "external" and self.cert_issuer:
            msg = "cert_issuer should be unspecified if cert_issuer_class is external"
            raise ValueError(msg)
        if (
            self.cert_issuer_class in ["cluster-issuer", "issuer"]
            and not self.cert_issuer
        ):
            msg = "cert_issuer must be set if using cert_issuer_class:"
            " cluster-issuer or issuer"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def check_listener_route_name(self):
        listener_names = [listener_config.name for listener_config in self.listeners]
        for route_config in self.routes:
            if route_config.listener_name not in listener_names:
                msg = f"listener_name : {route_config.listener_name}"
                " in route : {route_config.name} is not defined"
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def check_hostnames(self):
        listener_hostnames = {
            listener_config.hostname for listener_config in self.listeners
        }  # a set comprehension!
        route_hostnames = set()
        for route_config in self.routes:
            for hostname in route_config.hostnames:
                route_hostnames.add(hostname)
        if not listener_hostnames & route_hostnames:
            msg = "The set of listener hostnames must match the set of route hostnames."
            raise ValueError(msg)
        return self

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


class OLEKSGateway(pulumi.ComponentResource):
    gateway: kubernetes.apiextensions.CustomResource = None
    routes: ClassVar[list[kubernetes.apiextensions.CustomResource]] = []

    def __init__(
        self,
        name: str,
        gateway_config: OLEKSGatewayConfig,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__(
            "ol:infrastructure:aws:eks:OLEKSGateway",
            name,
            None,
            opts,
        )

        if gateway_config.cert_issuer and gateway_config.cert_issuer_class in [
            "issuer",
            "cluster-issuer",
        ]:
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
    account_id: str | PositiveInt
    cluster_name: str | pulumi.Output[str]
    cluster_identities: pulumi.Output
    description: str
    policy_operator: Literal["StringEquals", "StringLike"]
    role_name: str
    service_account_identifier: str | None = None
    service_account_name: str | None = None
    service_account_namespace: str | None = None
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @model_validator(mode="after")
    def check_service_account_args(self):
        if self.service_account_identifier and (
            self.service_account_name or self.service_account_namespace
        ):
            msg = "Only service_account_identifier OR (service_account_name "
            "AND service_account_namespace) should be specified."
            raise ValueError(msg)
        if not self.service_account_identifier and (
            not self.service_account_name or not self.service_account_namespace
        ):
            msg = "Both service_account_name and service_account_namespace "
            "should be specified."
            raise ValueError(msg)
        if (
            self.service_account_identifier
            and not self.service_account_identifier.startswith("system:serviceaccount:")
        ):
            msg = "If specifying service_account_identifier "
            "it should start with 'system:serviceaccount'"
            raise ValueError(msg)
        return self


class OLEKSTrustRole(pulumi.ComponentResource):
    """Component resource to create an IAM Trust Role that can be associated with a K8S
    service account
    """

    role: aws.iam.Role = None

    def __init__(
        self,
        name: str,
        role_config: OLEKSTrustRoleConfig,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__(
            "ol:infrastructure:aws:eks:OLEKSTrustRole",
            name,
            None,
            opts,
        )

        self.__service_account_identifier = (
            role_config.service_account_identifier
            or f"system.serviceaccount:{role_config.service_account_namespace}:{role_config.service_account_name}"  # noqa: E501
        )
        self.role = aws.iam.Role(
            f"{role_config.cluster_name}-{role_config.role_name}-trust-role",
            name=f"{role_config.cluster_name}-{role_config.role_name}-trust-role",
            path=f"/ol-infrastructure/eks/{role_config.cluster_name}/",
            assume_role_policy=role_config.cluster_identities.apply(
                lambda ids: oidc_trust_policy_template(
                    oidc_identifier=ids[0]["oidcs"][0]["issuer"],
                    account_id=str(role_config.account_id),
                    k8s_service_account_identifier=self.__service_account_identifier,
                    operator=role_config.policy_operator,
                )
            ),
            description=role_config.description,
            tags=role_config.tags,
            opts=pulumi.ResourceOptions(parent=self).merge(opts),
        )
