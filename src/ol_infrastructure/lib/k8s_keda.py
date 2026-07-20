"""Shared KEDA autoscaling helpers for webapp deployments.

Scales webapps on APISIX request-rate and p95 latency (via Prometheus) rather
than CPU alone, with a CPU trigger retained as a backstop. CPU is a poor proxy
for saturation in these applications: a request blocked on the database, edX, or
a search backend keeps CPU low while the request queue grows, so a CPU-only
scaler stays flat through exactly the overload it should be reacting to.

This module is the single source of truth for the pattern that was previously
duplicated between ``applications/edxapp/k8s_autoscaling.py`` and
``applications/mit_learn/k8s_autoscaling.py``.

**Requires APISIX.** Both Prometheus triggers query ``apisix_http_status`` /
``apisix_http_latency_bucket``. An application whose traffic does not traverse
APISIX emits neither series, and a KEDA Prometheus trigger against a metric that
does not exist reports an error rather than scaling. Confirm the application's
routes actually appear in ``count by (route) (apisix_http_status)`` before
adopting this; several apps declare an ``OLApisixRoute`` in Pulumi yet emit no
APISIX metrics in production.
"""

from typing import Any

import pulumi
import pulumi_kubernetes as kubernetes
from pulumi import ResourceOptions

from ol_infrastructure.components.services.k8s import (
    OLApplicationK8sKedaWebappScalingConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SResources,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo

PROMETHEUS_SERVER = "https://prometheus-prod-10-prod-us-central-0.grafana.net/api/prom"

# Requests per second per pod. Above this the scaler adds replicas.
DEFAULT_REQUESTS_THRESHOLD = "20"
# p95 latency in milliseconds.
DEFAULT_LATENCY_THRESHOLD = "2000"
DEFAULT_CPU_THRESHOLD = "60"


def create_webapp_prometheus_trigger_auth(  # noqa: PLR0913
    application_name: str,
    env_name: str,
    namespace: str,
    k8s_global_labels: dict[str, str],
    stack_info: StackInfo,
    vault_k8s_resources: OLVaultK8SResources,
) -> tuple[kubernetes.apiextensions.CustomResource, str]:
    """Create the Prometheus TriggerAuthentication for webapp KEDA scaling.

    Reads Grafana Cloud metrics credentials from Vault (``secret-global/grafana``)
    and exposes them to KEDA as a basic-auth TriggerAuthentication.

    Must be created *before* the OLApplicationK8s component instance so the
    returned name is available for the webapp_keda_config. A single
    TriggerAuthentication can be shared by multiple ScaledObjects in the same
    namespace (edxapp shares one between LMS and CMS).

    Returns:
        A tuple of (TriggerAuthentication resource, trigger authentication name).
    """
    auth_secret_name = f"{env_name}-{application_name}-webapp-prometheus-auth"
    trigger_auth_name = f"{env_name}-{application_name}-webapp-prometheus-auth-trigger"

    auth_secret = OLVaultK8SSecret(
        f"ol-{stack_info.env_prefix}-{application_name}-webapp-prometheus-auth-{stack_info.env_suffix}",
        OLVaultK8SStaticSecretConfig(
            name=auth_secret_name,
            namespace=namespace,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=auth_secret_name,
            labels=k8s_global_labels,
            mount="secret-global",
            mount_type="kv-v2",
            path="grafana",
            templates={
                "username": '{{ get .Secrets "k8s_monitoring_metrics_username" }}',
                "password": '{{ get .Secrets "k8s_monitoring_api_key" }}',
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True, depends_on=[vault_k8s_resources]
        ),
    )

    trigger_auth = kubernetes.apiextensions.CustomResource(
        f"ol-{stack_info.env_prefix}-{application_name}-webapp-prometheus-trigger-auth-{stack_info.env_suffix}",
        api_version="keda.sh/v1alpha1",
        kind="TriggerAuthentication",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=trigger_auth_name,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec={
            "secretTargetRef": [
                {"parameter": "username", "name": auth_secret_name, "key": "username"},
                {"parameter": "password", "name": auth_secret_name, "key": "password"},
            ]
        },
        opts=pulumi.ResourceOptions(depends_on=[auth_secret]),
    )

    return trigger_auth, trigger_auth_name


def build_webapp_keda_config(  # noqa: PLR0913
    trigger_auth_name: str,
    route_matcher: str,
    namespace: str,
    pod_matcher: str,
    container_name: str,
    requests_threshold: str = DEFAULT_REQUESTS_THRESHOLD,
    latency_threshold: str | None = DEFAULT_LATENCY_THRESHOLD,
    cpu_threshold: str = DEFAULT_CPU_THRESHOLD,
) -> OLApplicationK8sKedaWebappScalingConfig:
    """Build a KEDA ScaledObject config driven by APISIX request rate and latency.

    Args:
        trigger_auth_name: Name from create_webapp_prometheus_trigger_auth.
        route_matcher: PromQL matcher for the ``route`` label, WITHOUT quotes,
            applied with ``=~``. Aggregate every route serving the webapp so
            total traffic drives scaling rather than a single route. Verify the
            value against live metrics -- the label is
            ``<namespace>_<OLApisixRoute pulumi resource name>_<route_name>``,
            which is not derivable from the application name alone.
        namespace: Kubernetes namespace, for the per-pod request-rate divisor.
        pod_matcher: PromQL matcher for the ``pod`` label, applied with ``=~``,
            selecting the webapp pods that serve the routes above.
        container_name: Container the CPU backstop trigger measures.
        requests_threshold: Requests/sec per pod before scaling up.
        latency_threshold: p95 latency in ms before scaling up. Pass None to omit
            the latency trigger (appropriate where request duration is dominated
            by payload size rather than contention, e.g. edxapp CMS).
        cpu_threshold: CPU utilization percent for the backstop trigger.

    Returns:
        A populated OLApplicationK8sKedaWebappScalingConfig.
    """
    # Divide by pod count so the threshold is per-pod and stays meaningful as the
    # deployment scales. Without this, total request rate rises with replicas and
    # the scaler ratchets upward indefinitely.
    requests_query = (
        f'sum(rate(apisix_http_status{{route=~"{route_matcher}"}}[5m]))'
        f'/count(kube_pod_info{{job="integrations/kubernetes/kube-state-metrics",'
        f'namespace="{namespace}",pod=~"{pod_matcher}"}})'
    )
    latency_query = (
        f"histogram_quantile(0.95,sum(rate("
        f'apisix_http_latency_bucket{{route=~"{route_matcher}"}}[5m])) by (le))'
    )

    triggers: list[dict[str, Any]] = [
        {
            "type": "prometheus",
            "metadata": {
                "serverAddress": PROMETHEUS_SERVER,
                "query": requests_query,
                "threshold": requests_threshold,
                "authModes": "basic",
            },
        },
    ]
    if latency_threshold is not None:
        triggers.append(
            {
                "type": "prometheus",
                "metadata": {
                    "serverAddress": PROMETHEUS_SERVER,
                    "query": latency_query,
                    "threshold": latency_threshold,
                    "authModes": "basic",
                },
            }
        )
    # CPU backstop: keeps the deployment scaling if Prometheus or the APISIX
    # metrics pipeline is unavailable, which would otherwise leave the webapp
    # with no working scaler at all.
    triggers.append(
        {
            "type": "cpu",
            "metricType": "Utilization",
            "metadata": {
                "value": cpu_threshold,
                "containerName": container_name,
            },
        }
    )

    return OLApplicationK8sKedaWebappScalingConfig(
        scale_up_stabilization_seconds=60,
        scale_up_percent=50,
        scale_up_period_seconds=60,
        # Scale down slowly. These apps have expensive cold starts (migrations
        # check, static asset load, edX/Redis connection setup), so shedding
        # replicas quickly after a traffic spike tends to cause a second spike.
        scale_down_stabilization_seconds=300 * 5,  # 25 minutes
        scale_down_percent=10,
        scale_down_period_seconds=300 * 5,  # 25 minutes
        polling_interval=60,
        cooldown_period=300,
        trigger_authentication_name=trigger_auth_name,
        triggers=triggers,
    )
