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

from bridge.lib.magic_numbers import DEFAULT_DNS_PORT
from ol_infrastructure.lib.aws.iam_helper import oidc_trust_policy_template
from ol_infrastructure.lib.ol_types import AWSBase


class OLEKSPodSecurityGroupConfig(BaseModel):
    cluster_cidrs: Union[list[str], list[pulumi.Output[str]]]
    cluster_security_group_id: Union[str, pulumi.Output[str]]
    cluster_vpc_id: Union[str, pulumi.Output[str]]
    description: str
    egress_rules: list[aws.vpc.SecurityGroupEgressRuleArgs] = []
    ingress_rules: list[aws.vpc.SecurityGroupIngressRuleArgs] = []
    k8s_labels: Optional[dict[str, str]]
    label_selectors: dict[str, str]
    namespace: str
    tags: Optional[dict[str, str]] = {}

    model_config = ConfigDict(arbitrary_types_allowed=True)


class OLEKKSPodSecurityGroup(pulumi.ComponentResource):
    SecurityGroupPolicy: kubernetes.apiextensions.CustomResource = None

    def __init__(
        self,
        name: str,
        psg_config: OLEKSPodSecurityGroupConfig,
        opts: Optional[pulumi.ResourceOptions] = None,
    ):
        super().__init__(
            "ol:infrastructure:aws:eks:OLEKSPodSecurityGroup",
            name,
            None,
            opts,
        )

        pod_security_group = aws.ec2.SecurityGroup(
            f"ol-eks-pod-security-group-{name}",
            name=name,
            description=psg_config.description,
            vpc_id=psg_config.cluster_vpc_id,
            tags=psg_config.tags,
        )
        self.security_group_name = pod_security_group.name
        self.security_group_id = pod_security_group.id

        # Loop through the ingress + egress args to create the application rules
        for ingress_rule_args in psg_config.ingress_rules:
            aws.vpc.SecurityGroupIngressRule(
                "name",
                args=ingress_rule_args,
                opts=pulumi.ResourceOptions(
                    parent=self, depends_on=[pod_security_group]
                ).merge(opts),
            )
        for egress_rule_args in psg_config.egress_rules:
            aws.vpc.SecurityGroupEgressRule(
                "name",
                args=egress_rule_args,
                opts=pulumi.ResourceOptions(
                    parent=self, depends_on=[pod_security_group]
                ).merge(opts),
            )
        pulumi.info(str(psg_config.cluster_cidrs))
        # Create the rest of the rules needed by every pod-security-group
        # In theory, these two overlap each other?
        for i, cluster_cidr in psg_config.cluster_cidrs:
            aws.vpc.SecurityGroupIngressRule(
                f"psg-ingress-rule-pod-sg-from-cluster-cidrs-{name}-{i}",
                ip_protocol="-1",  # allows all protocols + all ports
                security_group_id=pod_security_group.id,
                cidr_ipv4=cluster_cidr,
                opts=pulumi.ResourceOptions(
                    parent=self, depends_on=[pod_security_group]
                ).merge(opts),
            )
        aws.vpc.SecurityGroupIngressRule(
            f"psg-ingress-rule-pod-sg-from-cluster-sg-{name}",
            ip_protocol="-1",  # allows all protocols + all ports
            security_group_id=pod_security_group.id,
            referenced_security_group_id=psg_config.cluster_security_group_id,
            opts=pulumi.ResourceOptions(
                parent=self, depends_on=[pod_security_group]
            ).merge(opts),
        )
        # default ALLOW ALL egress rule that pulumi doesn't create for us
        aws.vpc.SecurityGroupEgressRule(
            f"psg-egress-rule-allow-all-{name}",
            security_group_id=pod_security_group.id,
            ip_protocol="-1",
            cidr_ipv4="0.0.0.0/0",
            opts=pulumi.ResourceOptions(
                parent=self, depends_on=[pod_security_group]
            ).merge(opts),
        )
        # From this group to the cluster-sg for DNS
        aws.vpc.SecurityGroupEgressRule(
            f"psg-egress-rule-pod-sg-to-cluster-dns-tcp-{name}",
            security_group_id=pod_security_group.id,
            referenced_security_group_id=psg_config.cluster_security_group_id,
            ip_protocol="tcp",
            from_port=DEFAULT_DNS_PORT,
            to_port=DEFAULT_DNS_PORT,
            opts=pulumi.ResourceOptions(
                parent=self, depends_on=[pod_security_group]
            ).merge(opts),
        )
        aws.vpc.SecurityGroupEgressRule(
            f"psg-egress-rule-pod-sg-to-cluster-dns-udp-{name}",
            security_group_id=pod_security_group.id,
            referenced_security_group_id=psg_config.cluster_security_group_id,
            ip_protocol="udp",
            from_port=DEFAULT_DNS_PORT,
            to_port=DEFAULT_DNS_PORT,
            opts=pulumi.ResourceOptions(
                parent=self, depends_on=[pod_security_group]
            ).merge(opts),
        )

        kubernetes.apiextensions.CustomResource(
            "airbyte-data-integration-psg-attachment",
            api_version="vpcresources.k8s.aws/v1beta1",
            kind="SecurityGroupPolicy",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name="airbyte-data-ingest-security-group",
                namespace=psg_config.namespace,
                labels=psg_config.k8s_labels,
            ),
            spec={
                "podSelector": {
                    "matchLabels": psg_config.label_selectors,
                },
                "securityGroups": {"groupIds": [pod_security_group.id]},
            },
            opts=pulumi.ResourceOptions(
                parent=self, depends_on=[pod_security_group], delete_before_replace=True
            ).merge(opts),
        )


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
    certificate_secret_name: Optional[str]
    certificate_secret_namespace: Optional[str] = ""

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
    annotations: Optional[dict[str, str]] = None
    cert_issuer: Optional[str] = None
    cert_issuer_class: Optional[Literal["cluster-issuer", "issuer", "external"]] = (
        "cluster-issuer"
    )
    gateway_class_name: str = "traefik"
    gateway_name: str
    http_redirect: bool = True
    labels: Optional[dict[str, str]] = None
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
        opts: Optional[pulumi.ResourceOptions] = None,
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
    account_id: Union[str, PositiveInt]
    cluster_name: Union[str, pulumi.Output[str]]
    cluster_identities: pulumi.Output
    description: str
    policy_operator: Literal["StringEquals", "StringLike"]
    role_name: str
    service_account_identifier: Optional[str] = None
    service_account_name: Optional[str] = None
    service_account_namespace: Optional[str] = None
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
        opts: Optional[pulumi.ResourceOptions] = None,
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
