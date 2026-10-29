# ruff: noqa: E501, PLR0913
"""Configure and install the Apache APISIX Helm chart.

This module deploys the Apache APISIX chart from https://apache.github.io/apisix-helm-chart.
The chart values, Gateway API objects and global rules that local-dev shares
are built in apisix_definition.py; this module adds the EKS-specific inputs
and resources.
"""

import pulumi_eks as eks
import pulumi_fastly as fastly
import pulumi_kubernetes as kubernetes
from pulumi import Config, InvokeOptions, Output, ResourceOptions

from bridge.lib.magic_numbers import AWS_LOAD_BALANCER_NAME_MAX_LENGTH
from ol_infrastructure.infrastructure.aws.eks.apisix_definition import (
    APISIX_CHART_REPOSITORY,
    APISIX_RELEASE_NAME,
    DEFAULT_CPU_REQUEST,
    DEFAULT_MEMORY_REQUEST,
    GATEWAY_ERROR_PAGES_CONFIGMAP_NAME,
    GATEWAY_NAME,
    IDENTITY_HEADER_STRIP_RULE_NAME,
    ApisixGatewayConfig,
    ApisixGatewayService,
    ApisixHighAvailability,
    apisix_helm_values,
    gateway_class_spec,
    gateway_error_pages,
    gateway_spec,
    identity_header_strip_rule_spec,
)
from ol_infrastructure.lib.aws.eks_helper import (
    cached_image_uri,
)
from ol_infrastructure.lib.k8s_crds import adopt_helm_chart_crds
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import StackInfo

# Sent on every gateway response by a global rule.  Hosts behind Fastly keep
# whatever their own Fastly service sets, because Fastly's response header
# rules run after the origin's.  No includeSubDomains until the subdomains of
# every gateway host have been checked for anything still served over HTTP.
# Starts at one day so a mistake ages out of browsers quickly; raise it to a
# year (31536000) once it has run without problems.
GATEWAY_HSTS_HEADER_VALUE = "max-age=86400"
# The grafana k8s-monitoring stack's Alloy receiver, OTLP over HTTP. From there
# traces go through the tail-sampling pipeline to Grafana Cloud (grafana.py).
GRAFANA_ALLOY_OTLP_HTTP_ADDRESS = (
    "grafana-k8s-monitoring-alloy-receiver.grafana.svc.cluster.local:4318"
)


def setup_apisix(
    cluster_name: str,
    k8s_provider: kubernetes.Provider,
    operations_namespace: kubernetes.core.v1.Namespace,
    node_groups: list[eks.NodeGroupV2],
    gateway_api_crds,
    stack_info: StackInfo,
    k8s_global_labels: dict[str, str],
    operations_tolerations: list[dict[str, str]],
    versions: dict[str, str],
    eks_config: Config,
    target_vpc,
    aws_config: AWSBase,
    cluster: eks.Cluster,
    lb_controller,
    fastly_provider: fastly.Provider,
    vpa_release: kubernetes.helm.v3.Release,
):
    """
    Configure and install the Apache APISIX ingress controller.

    This deploys APISIX using the Apache chart from https://apache.github.io/apisix-helm-chart
    in standalone mode with YAML-based configuration provider and apisix-standalone ingress controller.

    Note: The "worker has not received configuration" message in /status/ready is expected during initial
    startup until the ingress controller pushes the first configuration via the Admin API.

    :param cluster_name: The name of the EKS cluster.
    :param k8s_provider: The Kubernetes provider for Pulumi.
    :param operations_namespace: The operations namespace object.
    :param node_groups: A list of EKS node groups.
    :param gateway_api_crds: The Gateway API CRDs.
    :param stack_info: Information about the current Pulumi stack.
    :param k8s_global_labels: A dictionary of global labels to apply to Kubernetes resources.
    :param operations_tolerations: A list of tolerations for scheduling on operations nodes.
    :param versions: A dictionary of component versions.
    :param eks_config: The EKS configuration object.
    :param target_vpc: The target VPC object.
    :param aws_config: The AWS configuration object.
    :param cluster: The EKS cluster object.
    :param lb_controller: The AWS Load Balancer Controller.
    :param fastly_provider: The Fastly provider instance.
    :param vpa_release: The VPA Helm release; ensures VPA CRDs exist before the VPA object is created.
    """
    apisix_domains = eks_config.get_object("apisix_domains") or []

    # APISIX chart uses a different chart version scheme
    # Chart version 2.17.x contains APISIX 3.18.x
    apisix_chart_version = versions["APISIX_CHART"]

    # Get Fastly IP ranges for trusted proxy configuration
    fastly_ips = Output.all(
        fastly.get_fastly_ip_ranges(
            opts=InvokeOptions(provider=fastly_provider)
        ).cidr_blocks,
        fastly.get_fastly_ip_ranges(
            opts=InvokeOptions(provider=fastly_provider)
        ).ipv6_cidr_blocks,
    ).apply(lambda blocks: [*blocks[0], *blocks[1]])

    gateway_config = ApisixGatewayConfig(
        env_suffix=stack_info.env_suffix,
        admin_key=eks_config.require_secret("apisix_admin_key"),
        viewer_key=eks_config.require_secret("apisix_viewer_key"),
        service=ApisixGatewayService(
            type="LoadBalancer",
            annotations={
                "external-dns.kubernetes.io/hostname": ",".join(apisix_domains),
                "service.beta.kubernetes.io/aws-load-balancer-name": f"{cluster_name}-apache-apisix"[
                    :AWS_LOAD_BALANCER_NAME_MAX_LENGTH
                ],
                "service.beta.kubernetes.io/aws-load-balancer-type": "external",
                "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "ip",
                "service.beta.kubernetes.io/aws-load-balancer-scheme": "internet-facing",
                "service.beta.kubernetes.io/aws-load-balancer-cross-zone-load-balancing-enabled": "true",
                # NLB target-type "ip" disables client IP preservation by
                # default (unlike "instance" targets). Without this, APISIX
                # only ever sees the NLB's own address as $remote_addr, which
                # breaks the "real-ip" plugin's trustedAddresses check (it can
                # never match a Fastly IP) and any per-client-IP logic
                # downstream (upstream chash/hashOn=remote_addr, rate
                # limiting, etc).
                "service.beta.kubernetes.io/aws-load-balancer-target-group-attributes": "preserve_client_ip.enabled=true",
                "service.beta.kubernetes.io/aws-load-balancer-subnets": target_vpc.apply(
                    lambda tvpc: ",".join(tvpc["k8s_public_subnet_ids"])
                ),
                "service.beta.kubernetes.io/aws-load-balancer-additional-resource-tags": ",".join(
                    [f"{k}={v}" for k, v in aws_config.tags.items()]
                ),
            },
        ),
        high_availability=ApisixHighAvailability(
            min_replicas=eks_config.get_int("apisix_min_replicas") or 3,
            max_replicas=eks_config.get_int("apisix_max_replicas") or 5,
            target_cpu_utilization_percentage=(
                eks_config.get_int("apisix_target_cpu_utilization_percentage") or 50
            ),
        ),
        ingress_controller_replicas=3,
        image_repository=cached_image_uri("apache/apisix"),
        tolerations=operations_tolerations,
        labels=k8s_global_labels,
        trusted_addresses=fastly_ips,
        # The Grafana Alloy receiver the traces go to only exists where the
        # grafana k8s-monitoring stack is installed, which skips CI (see
        # substructure/aws/eks/grafana.py).
        otel_collector_address=(
            None
            if stack_info.env_suffix.lower() == "ci"
            else GRAFANA_ALLOY_OTLP_HTTP_ADDRESS
        ),
        service_monitor_enabled=True,
        cpu_request=eks_config.get("apisix_cpu") or DEFAULT_CPU_REQUEST,
        memory_request=eks_config.get("apisix_memory") or DEFAULT_MEMORY_REQUEST,
        memory_limit=eks_config.get("apisix_memory_limit"),
    )

    error_pages_configmap = kubernetes.core.v1.ConfigMap(
        f"{cluster_name}-apisix-error-pages-configmap",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=GATEWAY_ERROR_PAGES_CONFIGMAP_NAME,
            namespace="operations",
            labels=k8s_global_labels,
        ),
        data=gateway_error_pages(),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
        ),
    )

    # The apisix-ingress-controller subchart ships its CRDs in crds/, which Helm
    # only ever writes on install, so they froze at whatever chart version first
    # landed on each cluster while APISIX_CHART was upgraded repeatedly.
    # backendtrafficpolicies was 30 schema properties behind on all 12 clusters and
    # apisixupstreams 18 on most.
    #
    # Only the apisix.apache.org group. The subchart also ships gwapi-crds.yaml,
    # holding the Gateway API CRDs at channel "standard", bundle v1.6.0 -- the same
    # cluster-scoped objects setup_traefik installs from the *experimental* bundle
    # at GATEWAY_API_VERSION and owns. Adopting those here would force every
    # gateway.networking.k8s.io CRD down from experimental to standard and leave
    # two Pulumi resources overwriting each other on every up. skip_crds below
    # keeps Helm from installing that copy on a fresh cluster too, which the
    # release's existing depends_on gateway_api_crds already makes unnecessary.
    apisix_crds = adopt_helm_chart_crds(
        f"{cluster_name}-apisix-crds",
        kubeconfig=cluster.kubeconfig,
        repo=APISIX_CHART_REPOSITORY,
        chart="apisix",
        version=apisix_chart_version,
        groups={"apisix.apache.org"},
        opts=ResourceOptions(parent=operations_namespace, depends_on=[cluster]),
    )

    apisix_helm_release = kubernetes.helm.v3.Release(
        f"{cluster_name}-apisix-official-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name=APISIX_RELEASE_NAME,
            version=apisix_chart_version,
            namespace="operations",
            cleanup_on_fail=True,
            max_history=10,
            chart="apisix",
            # The CRDs are applied above as their own Pulumi resource. Helm would
            # only write them on a fresh install anyway, which is the bug.
            skip_crds=True,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo=APISIX_CHART_REPOSITORY,
            ),
            values=apisix_helm_values(gateway_config),
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            delete_before_replace=True,
            depends_on=[
                cluster,
                *node_groups,
                operations_namespace,
                gateway_api_crds,
                apisix_crds,
                lb_controller,
                error_pages_configmap,
            ],
        ),
    )

    # Standalone PDB for the ingress-controller deployment.
    # The sub-chart's built-in PDB (podDisruptionBudget.enabled) is disabled because
    # apisix-ingress-controller v1.1.1 has a template name mismatch: pdb.yaml calls
    # "apisix-ingress-controller.fullname" but _helpers.tpl only defines
    # "apisix-ingress-controller-manager.*" helpers, causing a Helm render failure.
    # We replicate the same policy here using the actual pod selector labels:
    #   app.kubernetes.io/name: apisix-ingress-controller  (chart name)
    #   app.kubernetes.io/instance: apache-apisix           (release name)
    kubernetes.policy.v1.PodDisruptionBudget(
        f"{cluster_name}-apisix-ingress-controller-pdb",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="apache-apisix-apisix-ingress-controller",
            namespace="operations",
            labels=k8s_global_labels,
        ),
        spec=kubernetes.policy.v1.PodDisruptionBudgetSpecArgs(
            max_unavailable=1,
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels={
                    "app.kubernetes.io/name": "apisix-ingress-controller",
                    "app.kubernetes.io/instance": "apache-apisix",
                },
            ),
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            depends_on=[apisix_helm_release],
        ),
    )

    # See identity_header_strip_rule_spec for why this is a global rule.
    kubernetes.apiextensions.CustomResource(
        f"{cluster_name}-apisix-identity-header-strip-global-rule",
        api_version="apisix.apache.org/v2",
        kind="ApisixGlobalRule",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=IDENTITY_HEADER_STRIP_RULE_NAME,
            namespace="operations",
            labels=k8s_global_labels,
        ),
        spec=identity_header_strip_rule_spec(),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            delete_before_replace=True,
            depends_on=[apisix_helm_release],
        ),
    )

    # HSTS on every gateway response (hq#14079).  A global rule rather than the
    # shared plugin configs' response-rewrite default, for the same two reasons
    # as identity-header-strip (see identity_header_strip_rule_spec): routes
    # with no plugin config would never get it, and a route carrying its own
    # response-rewrite (mitxonline, xpro, micromasters,
    # ocw-studio, jupyterhub) replaces the shared one by name.  A global
    # response-rewrite does not displace a route's: response-rewrite has no
    # run_policy="prefer_route", so APISIX runs the global copy and then the
    # route's in header_filter, and each caches its header operations under its
    # own conf_type ("global_rule" vs "route").  The two share only
    # ctx.response_rewrite_matched, which the route's header_filter recomputes
    # before its body_filter reads it, and this copy has no body or filters, so
    # its own body_filter does nothing.
    #
    # Its own ApisixGlobalRule so it can be removed without touching the
    # identity-header strip; no other global rule uses response-rewrite, so the
    # controller's plugin-name-keyed flattening has nothing to collide with.
    kubernetes.apiextensions.CustomResource(
        f"{cluster_name}-apisix-security-response-headers-global-rule",
        api_version="apisix.apache.org/v2",
        kind="ApisixGlobalRule",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="security-response-headers",
            namespace="operations",
            labels=k8s_global_labels,
        ),
        spec={
            "ingressClassName": "apache-apisix",
            "plugins": [
                {
                    "name": "response-rewrite",
                    "enable": True,
                    "config": {
                        "headers": {
                            "set": {
                                "Strict-Transport-Security": GATEWAY_HSTS_HEADER_VALUE,
                            }
                        }
                    },
                }
            ],
        },
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            depends_on=[apisix_helm_release],
        ),
    )

    # Create GatewayClass resource for Gateway API
    gateway_class = kubernetes.apiextensions.CustomResource(
        f"{cluster_name}-gateway-class",
        api_version="gateway.networking.k8s.io/v1",
        kind="GatewayClass",
        metadata={"name": GATEWAY_NAME},
        spec=gateway_class_spec(),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            depends_on=[gateway_api_crds],
        ),
    )

    # Create Gateway resource for Gateway API
    apisix_gateway = kubernetes.apiextensions.CustomResource(
        f"{cluster_name}-gateway",
        api_version="gateway.networking.k8s.io/v1",
        kind="Gateway",
        metadata={
            "name": GATEWAY_NAME,
            "namespace": "operations",
        },
        spec=gateway_spec(),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            depends_on=[gateway_api_crds, gateway_class],
        ),
    )

    # Get the APISIX LoadBalancer service to retrieve its hostname
    apisix_gateway_svc = kubernetes.core.v1.Service.get(
        f"{cluster_name}-apisix-gateway-svc",
        id="operations/apache-apisix-gateway",
        opts=ResourceOptions(provider=k8s_provider),
    )

    # Workaround: APISIX ingress controller 2.0.0-rc5 doesn't populate Gateway status.addresses
    # from publishService in GatewayProxy spec. Without status.addresses, external-dns cannot
    # discover the LoadBalancer hostname to create DNS records for HTTPRoutes attached to Gateway.
    #
    # Upstream issue: https://github.com/apache/apisix-ingress-controller/issues/2643
    #
    # Solution: Use external-dns.kubernetes.io/target annotation on Gateway resource.
    # ExternalDNS checks this annotation first before looking at status.addresses.
    # See: https://github.com/kubernetes-sigs/external-dns/blob/master/docs/sources/gateway-api.md
    #
    # Patch the Gateway with the LoadBalancer hostname annotation
    kubernetes.apiextensions.CustomResourcePatch(
        f"{cluster_name}-gateway-annotation-patch",
        api_version="gateway.networking.k8s.io/v1",
        kind="Gateway",
        metadata=apisix_gateway_svc.status.load_balancer.ingress[0].hostname.apply(
            lambda hostname: {
                "name": "apisix",
                "namespace": "operations",
                "annotations": {
                    "pulumi.com/patchForce": "true",
                    "external-dns.kubernetes.io/target": hostname,
                },
            }
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=apisix_gateway,
            depends_on=[apisix_gateway, apisix_gateway_svc],
        ),
    )

    # Create a VerticalPodAutoscaler for the APISIX gateway deployment.
    #
    # VPA manages memory sizing only; CPU-based horizontal scaling is handled by the HPA
    # above (targetCPUUtilizationPercentage/minReplicas/maxReplicas, configurable via
    # apisix_target_cpu_utilization_percentage/apisix_min_replicas/apisix_max_replicas,
    # defaulting to 50/3/5). Splitting authority this way avoids the known HPA/VPA
    # conflict: VPA adjusting CPU requests would distort the CPU utilization
    # percentage that the HPA observes.
    #
    # updateMode "InPlaceOrRecreate" attempts to resize memory without evicting the pod
    # (K8s 1.33+, VPA 1.6+), falling back to eviction only when an in-place update is
    # not possible. The PodDisruptionBudget (maxUnavailable: 1) ensures at most one pod
    # is disrupted at a time if eviction is needed, protecting gateway availability.
    #
    # controlledValues "RequestsAndLimits" scales the limit proportionally to
    # whatever request/limit ratio is set on the Deployment (see the pod spec's
    # resources block above, which now sets limit = 2x request by default) --
    # VPA raises the request toward maxAllowed based on observed usage and
    # keeps that same burst-headroom ratio on the limit as it does.
    kubernetes.apiextensions.CustomResource(
        f"{cluster_name}-apisix-vpa",
        api_version="autoscaling.k8s.io/v1",
        kind="VerticalPodAutoscaler",
        metadata={
            "name": "apache-apisix",
            "namespace": "operations",
        },
        spec={
            "targetRef": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": "apache-apisix",
            },
            "updatePolicy": {
                "updateMode": "InPlaceOrRecreate",
            },
            "resourcePolicy": {
                "containerPolicies": [
                    {
                        "containerName": "apisix",
                        # Only control memory so the HPA retains full authority
                        # over CPU requests and therefore replica count decisions.
                        "controlledResources": ["memory"],
                        "controlledValues": "RequestsAndLimits",
                        "minAllowed": {
                            "memory": gateway_config.memory_request,
                        },
                        "maxAllowed": {
                            "memory": eks_config.get("apisix_max_memory") or "3Gi",
                        },
                    }
                ]
            },
        },
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            # vpa_release ensures the autoscaling.k8s.io CRDs are installed first.
            # apisix_helm_release ensures the target Deployment exists before VPA targets it.
            depends_on=[vpa_release, apisix_helm_release],
        ),
    )
