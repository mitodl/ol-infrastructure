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


class OLEKSGatewayRateLimitConfig(BaseModel):
    """Per-client-IP rate limiting for Traefik HTTPRoutes (DDoS backstop).

    Materialised as a Traefik ``Middleware`` CRD referenced from every route via
    an ExtensionRef filter.  Limits are enforced per Traefik pod, so the effective
    ceiling scales with the controller replica count; this throttles single-source
    floods rather than replacing edge/volumetric protection.
    """

    # Thresholds are deliberately generous: large shared-NAT egress points (most
    # notably the MIT campus network) collapse many legitimate users onto a single
    # public IP, so a tight per-IP ceiling would throttle real traffic at peak.
    # Average number of requests permitted per ``period``.
    average: PositiveInt = 300
    # Maximum burst of requests tolerated above the average before throttling.
    burst: PositiveInt = 600
    period: str = "1s"
    # Depth of the client IP within X-Forwarded-For, counted from the right.
    # 1 matches a single trusted CDN hop (Fastly), whose X-Forwarded-For the
    # Traefik controller trusts (see forwardedHeaders.trustedIPs), so the bucket
    # keys on the real client IP rather than the Fastly edge address.
    ip_strategy_depth: PositiveInt = 1

    @model_validator(mode="after")
    def check_rate_limit_thresholds(self) -> "OLEKSGatewayRateLimitConfig":
        if self.burst < self.average:
            msg = "rate_limit.burst must be >= rate_limit.average"
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
    rate_limit: OLEKSGatewayRateLimitConfig | None = None

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

        # Materialise a Traefik Middleware that applies per-client-IP rate
        # limiting, then reference it from every route via an ExtensionRef
        # filter.  The http-redirect route above is intentionally left out: it
        # only issues a 301 and never reaches a backend, so throttling it would
        # add no protection.
        rate_limit_filters = self.__build_rate_limit_filters(gateway_config, opts)

        for route_config in gateway_config.routes:
            # Build the rule configuration
            rule = {
                "filters": [*(route_config.filters or []), *rate_limit_filters],
                "matches": route_config.matches,
            }

            # Only add backendRefs if backend service is specified
            # (RequestRedirect filters don't need a backend)
            if route_config.backend_service_name is not None:
                rule["backendRefs"] = [
                    {
                        "name": route_config.backend_service_name,
                        "namespace": route_config.backend_service_namespace,
                        "kind": "Service",
                        "port": route_config.backend_service_port,
                    }
                ]

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
                "rules": [rule],
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

    def __build_rate_limit_filters(
        self,
        gateway_config: OLEKSGatewayConfig,
        opts: pulumi.ResourceOptions | None,
    ) -> list[dict[str, Any]]:
        """Create the rate-limit Middleware and return its ExtensionRef filter.

        Returns an empty list when no rate limit is configured, so callers can
        unconditionally splat the result into a route's filter list.
        """
        if gateway_config.rate_limit is None:
            return []
        rate_limit_middleware_name = f"{gateway_config.gateway_name}-ratelimit"
        kubernetes.apiextensions.CustomResource(
            f"{gateway_config.gateway_name}-ratelimit-middleware-resource",
            api_version="traefik.io/v1alpha1",
            kind="Middleware",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name=rate_limit_middleware_name,
                labels=gateway_config.labels,
                namespace=gateway_config.namespace,
            ),
            spec={
                "rateLimit": {
                    "average": gateway_config.rate_limit.average,
                    "burst": gateway_config.rate_limit.burst,
                    "period": gateway_config.rate_limit.period,
                    "sourceCriterion": {
                        "ipStrategy": {
                            "depth": gateway_config.rate_limit.ip_strategy_depth,
                        },
                    },
                },
            },
            opts=pulumi.ResourceOptions(parent=self).merge(opts),
        )
        return [
            {
                "type": "ExtensionRef",
                "extensionRef": {
                    "group": "traefik.io",
                    "kind": "Middleware",
                    "name": rate_limit_middleware_name,
                },
            }
        ]


class OLEKSTrustRoleConfig(AWSBase):
    account_id: str | PositiveInt
    cluster_name: str | pulumi.Output[str]
    cluster_identities: pulumi.Output
    description: str
    policy_operator: Literal["StringEquals", "StringLike"]
    role_name: str
    service_account_identifier: str | list[str] | None = None
    service_account_name: str | list[str] | None = None
    service_account_namespace: str | None = None
    # Maximum duration (seconds) for STS sessions issued via AssumeRoleWithWebIdentity.
    # AWS default is 3600 (1 hour); maximum is 43200 (12 hours).
    # Long-running pipeline workloads (e.g. large S3 bulk-loads) may need more than
    # 1 hour.  Set this to 43200 for Dagster or similar batch-processing deployments.
    max_session_duration: int = 3600
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
        # Validate service_account_identifier format(s)
        identifiers = (
            [self.service_account_identifier]
            if isinstance(self.service_account_identifier, str)
            else self.service_account_identifier or []
        )
        for identifier in identifiers:
            if not identifier.startswith("system:serviceaccount:"):
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

        # Build service account identifier(s)
        if role_config.service_account_identifier:
            service_account_identifiers = role_config.service_account_identifier
        else:
            # Convert service account name(s) to identifier(s)
            # Note: validation ensures service_account_name is not None here
            sa_name = role_config.service_account_name
            if sa_name is None:
                msg = (
                    "service_account_name must be provided when "
                    "service_account_identifier is not"
                )
                raise ValueError(msg)
            service_account_names: list[str] = (
                [sa_name] if isinstance(sa_name, str) else sa_name
            )
            service_account_identifiers = [
                f"system:serviceaccount:{role_config.service_account_namespace}:{sa_name}"
                for sa_name in service_account_names
            ]

        self.role = pulumi.Output.from_input(role_config.cluster_name).apply(
            lambda cluster_name: aws.iam.Role(
                f"{cluster_name}-{role_config.role_name}-trust-role",
                name=f"{cluster_name}-{role_config.role_name}-trust-role"[:63],
                path=f"/ol-infrastructure/eks/{cluster_name}/",
                assume_role_policy=role_config.cluster_identities.apply(
                    lambda ids: oidc_trust_policy_template(
                        oidc_identifier=ids[0]["oidcs"][0]["issuer"],
                        account_id=str(role_config.account_id),
                        k8s_service_account_identifier=service_account_identifiers,
                        operator=role_config.policy_operator,
                    )
                ),
                description=role_config.description,
                max_session_duration=role_config.max_session_duration,
                tags=role_config.tags,
                opts=pulumi.ResourceOptions(parent=self).merge(opts),
            )
        )
