# ruff: noqa: E501
"""The archive's one internal write endpoint (D7).

A Traefik release of its own in the archive namespace, behind an internal NLB
that only the peered VPCs' pod CIDRs can reach, terminating TLS with a
cert-manager certificate. Routes are Gateway API HTTPRoutes bound to a dedicated
GatewayClass, so the cluster's shared Traefik, which watches every namespace,
never picks them up. `/ready` proves the path end to end; each backend adds
its push routes with `push_route()` when it lands.
"""

from dataclasses import dataclass

import pulumi_kubernetes as kubernetes
from pulumi import Input, Output, Resource, ResourceOptions, StackReference
from pulumi_aws import ec2

from bridge.lib.magic_numbers import (
    AWS_LOAD_BALANCER_NAME_MAX_LENGTH,
    DEFAULT_HTTPS_PORT,
)
from bridge.lib.versions import TRAEFIK_CHART_VERSION
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.traefik import OLTraefikMiddleware
from ol_infrastructure.lib.aws.eks_helper import (
    GATEWAY_CLASS_LABEL,
    ecr_dockerhub_registry,
)
from ol_infrastructure.lib.ol_types import (
    AlertTier,
    AWSBase,
    Component,
    K8sGlobalLabels,
    Services,
    cluster_addon_labels,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo

RELEASE_NAME = "telemetry-archive-ingress"
GATEWAY_CLASS_NAME = "telemetry-archive"
GATEWAY_NAME = "telemetry-archive"
LISTENER_NAME = "websecure"
TLS_SECRET_NAME = "telemetry-archive-tls"  # noqa: S105  # pragma: allowlist secret
# The chart's websecure entrypoint. A Gateway listener binds to an entrypoint by
# port, so this is the container port, not the NLB's 443.
WEBSECURE_PORT = 8443
# The chart's `traefik` entrypoint, which serves /ping for the probes.
TRAEFIK_ENTRYPOINT_PORT = 8080

# VPCs whose EKS clusters run Alloy and will write to the archive from Phase 1.
WRITER_VPCS = ("applications_vpc", "data_vpc", "residential_mitx_vpc")


@dataclass
class ArchiveIngress:
    """What later phases attach routes and middleware to."""

    release: kubernetes.helm.v3.Release
    gateway: kubernetes.apiextensions.CustomResource
    push_auth_middleware: OLTraefikMiddleware
    security_group: ec2.SecurityGroup
    domain: str


def build(  # noqa: PLR0913
    *,
    stack_info: StackInfo,
    aws_config: AWSBase,
    k8s_labels: K8sGlobalLabels,
    k8s_global_labels: dict[str, str],
    namespace: str,
    domain: str,
    network_stack: StackReference,
    node_selector: dict[str, str],
    tolerations: list[dict[str, str]],
    push_auth_secret_name: str,
    depends_on: Input[list[Resource]],
) -> ArchiveIngress:
    """Build the NLB-fronted Traefik, its Gateway, TLS, auth middleware and /ready."""
    env = stack_info.env_suffix
    operations_vpc = network_stack.require_output("operations_vpc")
    operations_pod_cidrs = operations_vpc["k8s_pod_subnet_cidrs"]

    # Pod CIDRs of the writer clusters plus the whole operations VPC, which also
    # holds the EC2 hosts (Concourse, Consul, Vault) that ship through Vector.
    writer_cidrs = Output.all(
        operations_vpc["cidr"],
        *[
            network_stack.require_output(vpc)["k8s_pod_subnet_cidrs"]
            for vpc in WRITER_VPCS
        ],
    ).apply(lambda blocks: [blocks[0], *[cidr for vpc in blocks[1:] for cidr in vpc]])

    security_group = ec2.SecurityGroup(
        f"telemetry-archive-nlb-{env}",
        name=f"telemetry-archive-{env}-nlb",
        description="Access control for the telemetry archive write endpoint",
        vpc_id=operations_vpc["id"],
        ingress=[
            ec2.SecurityGroupIngressArgs(
                cidr_blocks=writer_cidrs,
                protocol="tcp",
                from_port=DEFAULT_HTTPS_PORT,
                to_port=DEFAULT_HTTPS_PORT,
                description="Telemetry writers in the peered VPCs",
            )
        ],
        # Declaring any rule drops AWS's default allow-all egress, and the NLB
        # needs to reach the Traefik pods (target-type ip) for forwarding and
        # health checks.
        egress=[
            ec2.SecurityGroupEgressArgs(
                cidr_blocks=operations_pod_cidrs,
                protocol="tcp",
                from_port=WEBSECURE_PORT,
                to_port=WEBSECURE_PORT,
                description="Forward to the Traefik pods",
            )
        ],
        tags=aws_config.tags,
    )

    release = kubernetes.helm.v3.Release(
        f"telemetry-archive-ingress-{env}",
        kubernetes.helm.v3.ReleaseArgs(
            name=RELEASE_NAME,
            chart="traefik",
            version=TRAEFIK_CHART_VERSION,
            namespace=namespace,
            # The cluster's Traefik already adopted the chart CRDs.
            skip_crds=True,
            cleanup_on_fail=True,
            max_history=10,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://traefik.github.io/charts",
            ),
            values={
                "image": {"registry": f"{ecr_dockerhub_registry()}/library"},
                "commonLabels": cluster_addon_labels(
                    base_labels=k8s_global_labels,
                    stack_info=stack_info,
                    service=Services.telemetry_archive,
                    component=Component.gateway,
                    alert_tier=AlertTier.ticket,
                ),
                "deployment": {"replicas": 2},
                "nodeSelector": node_selector,
                "tolerations": tolerations,
                # Nothing cluster-wide: no IngressClass, no dashboard route, and
                # a GatewayClass of its own so the shared Traefik ignores the
                # archive's Gateway and HTTPRoutes.
                "ingressClass": {"enabled": False},
                "ingressRoute": {"dashboard": {"enabled": False}},
                "gatewayClass": {
                    "enabled": True,
                    "name": GATEWAY_CLASS_NAME,
                    "labels": {GATEWAY_CLASS_LABEL: GATEWAY_CLASS_NAME},
                },
                # The Gateway is declared below so its listener can carry the
                # TLS certificate reference.
                "gateway": {"enabled": False},
                "providers": {
                    "kubernetesIngress": {"enabled": False},
                    "kubernetesCRD": {"enabled": True, "namespaces": [namespace]},
                    "kubernetesGateway": {
                        "enabled": True,
                        "namespaces": [namespace],
                        # Every Traefik instance answers to the same Gateway
                        # API controller name, so a distinct GatewayClass is
                        # not isolation by itself: the shared public Traefik
                        # would also load this Gateway onto its NLB. Both
                        # controllers select their own class by this label.
                        "labelSelector": f"{GATEWAY_CLASS_LABEL}={GATEWAY_CLASS_NAME}",
                        # The DNS record comes from the Service annotation
                        # below. Copying the NLB hostname onto the Gateway
                        # status would make external-dns's gateway source
                        # publish the same record a second time.
                        "statusAddress": {"service": {"enabled": False}},
                    },
                },
                "ports": {
                    # Internal, HTTPS-only: no :80 listener on the NLB at all.
                    "web": {"expose": {"default": False}},
                    "websecure": {
                        "port": WEBSECURE_PORT,
                        "expose": {"default": True},
                        "exposedPort": DEFAULT_HTTPS_PORT,
                    },
                },
                "log": {"level": "INFO"},
                "accessLog": {"enabled": True, "format": "json"},
                "metrics": {"prometheus": {"serviceMonitor": {"enabled": True}}},
                "resources": {
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"memory": "512Mi"},
                },
                "service": {
                    "annotations": {
                        "service.beta.kubernetes.io/aws-load-balancer-name": (
                            f"telemetry-archive-{env}"[
                                :AWS_LOAD_BALANCER_NAME_MAX_LENGTH
                            ]
                        ),
                        "service.beta.kubernetes.io/aws-load-balancer-type": "external",
                        "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "ip",
                        "service.beta.kubernetes.io/aws-load-balancer-scheme": "internal",
                        "service.beta.kubernetes.io/aws-load-balancer-cross-zone-load-balancing-enabled": "true",
                        "service.beta.kubernetes.io/aws-load-balancer-security-groups": security_group.id,
                        # The pod subnets, so the NLB's addresses fall inside
                        # the CIDRs the node security group already admits.
                        "service.beta.kubernetes.io/aws-load-balancer-subnets": operations_vpc[
                            "k8s_pod_subnet_ids"
                        ].apply(",".join),
                        "service.beta.kubernetes.io/aws-load-balancer-additional-resource-tags": ",".join(
                            f"{k}={v}" for k, v in aws_config.tags.items()
                        ),
                        # external-dns watches Services on every cluster, and the
                        # NLB is internal, so the record resolves to private
                        # addresses only.
                        "external-dns.kubernetes.io/hostname": domain,
                    },
                },
            },
        ),
        opts=ResourceOptions(depends_on=depends_on, delete_before_replace=True),
    )

    certificate = OLCertManagerCert(
        f"telemetry-archive-cert-{env}",
        cert_config=OLCertManagerCertConfig(
            application_name="telemetry-archive",
            k8s_namespace=namespace,
            k8s_labels=k8s_labels.model_dump(),
            dest_secret_name=TLS_SECRET_NAME,
            dns_names=[domain],
        ),
    )

    gateway = kubernetes.apiextensions.CustomResource(
        f"telemetry-archive-gateway-{env}",
        api_version="gateway.networking.k8s.io/v1",
        kind="Gateway",
        metadata={
            "name": GATEWAY_NAME,
            "namespace": namespace,
            "labels": k8s_labels.model_dump(),
        },
        spec={
            "gatewayClassName": GATEWAY_CLASS_NAME,
            "listeners": [
                {
                    "name": LISTENER_NAME,
                    "hostname": domain,
                    "port": WEBSECURE_PORT,
                    "protocol": "HTTPS",
                    "allowedRoutes": {"namespaces": {"from": "Same"}},
                    "tls": {
                        "mode": "Terminate",
                        "certificateRefs": [
                            {"kind": "Secret", "name": TLS_SECRET_NAME}
                        ],
                    },
                }
            ],
        },
        opts=ResourceOptions(depends_on=[release, certificate]),
    )

    # Phase 1 and 2 push routes attach this as an ExtensionRef filter.
    push_auth_middleware = OLTraefikMiddleware(
        f"telemetry-archive-push-auth-{env}",
        middleware_name="telemetry-archive-push-auth",
        namespace=namespace,
        spec={"basicAuth": {"secret": push_auth_secret_name, "removeHeader": True}},
        opts=ResourceOptions(depends_on=depends_on),
    )

    # `/ready` proves the whole path (DNS, NLB, TLS, Gateway, HTTPRoute) before
    # any backend exists: it is Traefik's own /ping, reached back through a
    # ClusterIP Service on its probe entrypoint.
    ping_service = kubernetes.core.v1.Service(
        f"telemetry-archive-ping-{env}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="telemetry-archive-ping",
            namespace=namespace,
            labels=k8s_labels.model_dump(),
        ),
        spec=kubernetes.core.v1.ServiceSpecArgs(
            selector={
                "app.kubernetes.io/name": "traefik",
                "app.kubernetes.io/instance": f"{RELEASE_NAME}-{namespace}",
            },
            ports=[
                kubernetes.core.v1.ServicePortArgs(
                    name="traefik",
                    port=TRAEFIK_ENTRYPOINT_PORT,
                    target_port="traefik",
                )
            ],
        ),
        opts=ResourceOptions(depends_on=[release]),
    )
    kubernetes.apiextensions.CustomResource(
        f"telemetry-archive-ready-route-{env}",
        api_version="gateway.networking.k8s.io/v1",
        kind="HTTPRoute",
        metadata={
            "name": "telemetry-archive-ready",
            "namespace": namespace,
            "labels": k8s_labels.model_dump(),
        },
        spec={
            "parentRefs": [{"name": GATEWAY_NAME, "sectionName": LISTENER_NAME}],
            "hostnames": [domain],
            "rules": [
                {
                    "matches": [{"path": {"type": "Exact", "value": "/ready"}}],
                    "filters": [
                        {
                            "type": "URLRewrite",
                            "urlRewrite": {
                                "path": {
                                    "type": "ReplaceFullPath",
                                    "replaceFullPath": "/ping",
                                }
                            },
                        }
                    ],
                    "backendRefs": [
                        {
                            "name": "telemetry-archive-ping",
                            "port": TRAEFIK_ENTRYPOINT_PORT,
                        }
                    ],
                }
            ],
        },
        opts=ResourceOptions(depends_on=[gateway, ping_service]),
    )

    return ArchiveIngress(
        release=release,
        gateway=gateway,
        push_auth_middleware=push_auth_middleware,
        security_group=security_group,
        domain=domain,
    )


def push_route(  # noqa: PLR0913
    *,
    name: str,
    stack_info: StackInfo,
    namespace: str,
    k8s_labels: K8sGlobalLabels,
    ingress: ArchiveIngress,
    paths: list[str],
    backend_service: str,
    backend_port: int,
    depends_on: list[Resource],
) -> kubernetes.apiextensions.CustomResource:
    """Route exact push paths to a backend behind the basic-auth middleware.

    Exact matches only: a backend's other endpoints (query, ring, ready) stay
    unreachable from outside the cluster even with the credential.
    """
    return kubernetes.apiextensions.CustomResource(
        f"telemetry-archive-{name}-route-{stack_info.env_suffix}",
        api_version="gateway.networking.k8s.io/v1",
        kind="HTTPRoute",
        metadata={
            "name": f"telemetry-archive-{name}",
            "namespace": namespace,
            "labels": k8s_labels.model_dump(),
        },
        spec={
            "parentRefs": [{"name": GATEWAY_NAME, "sectionName": LISTENER_NAME}],
            "hostnames": [ingress.domain],
            "rules": [
                {
                    "matches": [
                        {"path": {"type": "Exact", "value": path}} for path in paths
                    ],
                    "filters": [ingress.push_auth_middleware.gateway_filter],
                    "backendRefs": [{"name": backend_service, "port": backend_port}],
                }
            ],
        },
        opts=ResourceOptions(
            depends_on=[
                ingress.gateway,
                ingress.push_auth_middleware,
                *depends_on,
            ]
        ),
    )
