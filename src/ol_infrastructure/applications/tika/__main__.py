"""Create the resources needed to run a tika server.  # noqa: D200"""

import json
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions

from bridge.lib.versions import TIKA_CHART_VERSION
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.services.apisix import (
    OLApisixPluginConfig,
    OLApisixRoute,
    OLApisixRouteConfig,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
    OLApisixUpstream,
    OLApisixUpstreamConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    Application,
    BusinessUnit,
    K8sAppLabels,
    Product,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import (
    make_stack_reference,
    parse_stack,
)
from ol_infrastructure.lib.vault import setup_vault_provider

##################################
##    Setup + Config Retrival   ##
##################################

stack_info = parse_stack()
tika_config = Config("tika")

# Setup K8S provider for Kubernetes deployment
cluster_stack = make_stack_reference(projects.EKS, f"applications.{stack_info.name}")
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

# Load X-Access-Token from secrets
x_access_token = read_yaml_secrets(Path(f"tika/tika.{stack_info.env_suffix}.yaml"))[
    "x_access_token"
]

if Config("vault_server").get("env_namespace"):
    setup_vault_provider()

# Store the access token in vault
vault.generic.Secret(
    "tika-server-x-access-token-vault-secret",
    path="secret-operations/tika/access-token",
    data_json=json.dumps({"value": x_access_token}),
)

###################################
#   Kubernetes Deployment (K8S)   #
###################################

# Setup K8s namespace
tika_namespace = "tika"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(tika_namespace, ns)
)

# K8s labels for Tika
k8s_global_labels = K8sAppLabels(
    application=Application.tika,
    service=Services.tika,
    product=Product.data,
    ou=BusinessUnit.operations,
    source_repository="https://apache.jfrog.io/artifactory/tika",
    stack=stack_info,
).model_dump()

application_labels = k8s_global_labels | {
    "ol.mit.edu/application": "tika",
}

# Get K8s domain from config
tika_k8s_domain = tika_config.require("k8s_domain")

# Create TLS certificate for K8s deployment
tls_secret_name = "tika-tls-pair"  # pragma: allowlist secret  # noqa: S105
tika_cert = OLCertManagerCert(
    f"ol-tika-tls-cert-{stack_info.env_suffix}",
    cert_config=OLCertManagerCertConfig(
        application_name="tika",
        k8s_namespace=tika_namespace,
        k8s_labels=k8s_global_labels,
        create_apisixtls_resource=True,
        apisixtls_ingress_class=tika_config.get("apisix_ingress_class")
        or "apache-apisix",
        dest_secret_name=tls_secret_name,
        dns_names=[
            tika_k8s_domain,
        ],
    ),
)

# Create Kubernetes secret for X-Access-Token
tika_access_token_secret = kubernetes.core.v1.Secret(
    f"tika-access-token-secret-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="tika-access-token",
        namespace=tika_namespace,
        labels=application_labels,
    ),
    string_data={
        "x-access-token": x_access_token,
    },
    opts=ResourceOptions(
        delete_before_replace=True,
    ),
)

# Deploy Tika using Helm chart
tika_helm_release = kubernetes.helm.v3.Release(
    f"tika-{stack_info.env_suffix}-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="tika",
        chart="tika",
        version=TIKA_CHART_VERSION,
        namespace=tika_namespace,
        cleanup_on_fail=True,
        max_history=10,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://apache.jfrog.io/artifactory/tika",
        ),
        values={
            "commonLabels": k8s_global_labels,
            # Batch callers (Dagster's openedx document extraction, mit-learn's
            # import_all_mit_edx_files, OCW) aren't coordinated. On 2026-10-20
            # an ~11h Dagster batch ran three fixed replicas at 2-3 cores each,
            # then at their 4-core limit for the last ~3h; that day had 179
            # parser TIMEOUTs and two OOMKills. Enabling the chart's HPA also drops
            # `replicas` from the Deployment, which Helm's three-way merge
            # would otherwise reset on every upgrade. Utilization is relative
            # to the 1-core request, so 200% is 2 cores per pod. Memory is left
            # out: in that batch it rose and fell with CPU, so it adds no
            # earlier signal. The default scale-down behavior (300s
            # stabilization) is kept; the chart can't set `behavior`.
            "autoscaling": {
                "enabled": True,
                "minReplicas": 3,
                "maxReplicas": 6,
                "targetCPUUtilizationPercentage": 200,
                "targetMemoryUtilizationPercentage": 0,
            },
            # The HTTP server runs in the forked TikaServerProcess, so while the
            # watchdog restarts it (1-24s, measured 2026-10-02) the port refuses
            # connections. The chart default needs 5s x 20 failures (100s) to
            # mark a pod unready, longer than any of those restarts; a refused
            # connection fails instantly, so this takes ~6s. The timeout keeps
            # the chart's 30s because loaded pods have answered `/` slower than
            # that (18 "deadline exceeded" probe events in the 14 days to
            # 2026-10-21); a shorter one could pull every busy pod at once.
            # Liveness keeps the chart's lenient default so a restart isn't a
            # kill.
            "readinessProbe": {
                "periodSeconds": 2,
                "timeoutSeconds": 30,
                "failureThreshold": 3,
            },
            "service": {
                "type": "ClusterIP",
                "port": 9998,
            },
            # Idle most of the day, then batch jobs pin every pod. The chart's
            # default limits.cpu of 2 merges under these values unless
            # overridden; at 2 cores pods were CFS-throttled in ~60% of
            # periods during the 2026-10-02 backfill and the parser watchdog
            # restarted the forked JVM on TIMEOUT. 4 cores lets a burst finish.
            "resources": {
                "requests": {
                    "cpu": "1",
                    "memory": "6Gi",  # java never gives it back
                },
                "limits": {
                    "cpu": "4",
                    "memory": "6Gi",
                },
            },
            # By default the JVM's container-aware ergonomics only give the
            # heap 25% of the container memory limit, leaving the rest
            # unclaimed while Metaspace/direct memory have no cap and can grow
            # unbounded. That combination lets a single large document balloon
            # off-heap and get the whole container OOMKilled by the kernel
            # rather than the JVM raising a catchable OutOfMemoryError.
            # Budget the JVM at 2048Mi heap + 320Mi metaspace + 320Mi direct
            # memory (2688Mi). The rest of the container limit is headroom
            # outside the JVM: Tika's default PDF OCR strategy is AUTO, which
            # auto-invokes the `tesseract` binary as a native subprocess for
            # scanned/image PDF pages, and that subprocess's memory isn't
            # bounded by any JAVA_TOOL_OPTIONS flag.
            # 2026-10-14: heap 1152m->2g, limit 4Gi->6Gi. With the 1152m heap,
            # on 2026-10-02 the watchdog restarted the forked parser 128 times
            # with status ERROR and Tika logged 276 "OutOfMemoryError: Java
            # heap space" lines. Container RSS peaked at ~3970Mi of 4Gi in the
            # 30 days to 2026-10-14, so the heap could only grow if the limit
            # did. The extra 2Gi covers the 896Mi heap increase and
            # keeps the ~2.3Gi non-JVM headroom the 4Gi limit was sized for.
            "env": [
                {
                    "name": "JAVA_TOOL_OPTIONS",
                    "value": (
                        "-Xmx2g -XX:MaxMetaspaceSize=320m -XX:MaxDirectMemorySize=320m"
                    ),
                },
            ],
        },
        skip_await=False,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
    ),
)

# The chart exposes neither lifecycle hooks nor the grace period. On SIGTERM the
# Tika watchdog's shutdown hook force-kills the forked server, so a scale-down
# would drop every in-flight parse. The preStop sleep runs before SIGTERM and
# keeps the pod serving those parses (up to the 300s APISIX read timeout) after
# it has left the Service endpoints.
tika_prestop_sleep_seconds = 300
kubernetes.apps.v1.DeploymentPatch(
    f"tika-{stack_info.env_suffix}-graceful-shutdown-patch",
    metadata=kubernetes.meta.v1.ObjectMetaPatchArgs(
        name="tika",
        namespace=tika_namespace,
        annotations={"pulumi.com/patchForce": "true"},
    ),
    spec=kubernetes.apps.v1.DeploymentSpecPatchArgs(
        template=kubernetes.core.v1.PodTemplateSpecPatchArgs(
            spec=kubernetes.core.v1.PodSpecPatchArgs(
                termination_grace_period_seconds=tika_prestop_sleep_seconds + 30,
                containers=[
                    kubernetes.core.v1.ContainerPatchArgs(
                        name="tika",
                        lifecycle=kubernetes.core.v1.LifecyclePatchArgs(
                            pre_stop=kubernetes.core.v1.LifecycleHandlerPatchArgs(
                                sleep=kubernetes.core.v1.SleepActionPatchArgs(
                                    seconds=tika_prestop_sleep_seconds
                                )
                            )
                        ),
                    )
                ],
            )
        )
    ),
    opts=ResourceOptions(depends_on=[tika_helm_release]),
)

# Endpoint granularity on the route gives APISIX one node per pod, so its
# default retries now apply. Nginx treats PUT as idempotent, so a parse that hit
# the 300s read timeout would be re-sent and start a second heavy parse on
# another pod while Tika is already overloaded. Callers own retrying.
OLApisixUpstream(
    name=f"tika-apisix-upstream-{stack_info.env_suffix}",
    upstream_config=OLApisixUpstreamConfig(
        service_name="tika",
        k8s_namespace=tika_namespace,
        k8s_labels=application_labels,
        retries=0,
    ),
)

# Create shared APISIX plugins for Tika
tika_shared_plugins = OLApisixSharedPlugins(
    name=f"tika-apisix-shared-plugins-{stack_info.env_suffix}",
    plugin_config=OLApisixSharedPluginsConfig(
        application_name="tika",
        resource_suffix="shared-plugins",
        k8s_namespace=tika_namespace,
        k8s_labels=application_labels,
        # Tika is called server-to-server with an X-Access-Token header (see the
        # route below); no browser page ever fetches it, so it needs no
        # Access-Control-* headers.  An empty list attaches no cors plugin.
        cors_allow_origins=[],
        enable_defaults=True,
    ),
)

# Create APISIX route with header token authentication
# Using serverless-pre-function to validate the X-Access-Token header
tika_apisix_route = OLApisixRoute(
    name=f"tika-apisix-route-{stack_info.env_suffix}",
    k8s_namespace=tika_namespace,
    k8s_labels=application_labels,
    ingress_class_name=tika_config.get("apisix_ingress_class") or "apache-apisix",
    route_configs=[
        OLApisixRouteConfig(
            route_name="tika-all",
            priority=10,
            shared_plugin_config_name=tika_shared_plugins.resource_name,
            timeout_send="300s",  # Tika can take a while to process large files
            # A slow parse hits the read timeout, not the send timeout. Left at
            # the 60s default, the gateway answers 504 long before the client's
            # own 300s timeout.
            timeout_read="300s",
            plugins=[
                # Use serverless-pre-function to validate X-Access-Token header
                # This is simpler than request-validation and more flexible
                OLApisixPluginConfig(
                    name="serverless-pre-function",
                    config={
                        "phase": "access",
                        "functions": [
                            f"""
return function(conf, ctx)
    local core = require("apisix.core")
    local token = core.request.header(ctx, "X-Access-Token")
    local expected = "{x_access_token}"
    if not token or token ~= expected then
        return 401, {{error = "Unauthorized"}}
    end
end
                            """.strip()
                        ],
                    },
                ),
            ],
            hosts=[tika_k8s_domain],
            paths=["/*"],
            backend_service_name="tika",
            backend_service_port=9998,
            # With "service" granularity APISIX reuses keepalive connections
            # to the ClusterIP that conntrack has pinned to one pod, so a pod
            # that is unready or in its preStop sleep keeps getting new parses.
            # Resolving to pod IPs lets APISIX drop it from the upstream.
            backend_resolve_granularity="endpoint",
        ),
    ],
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[tika_helm_release, tika_access_token_secret],
    ),
)
