# ruff: noqa: E501
"""KEDA autoscaling resources for the mit-learn webapp deployment.

Scales the webapp on APISIX request-rate and p95 latency (Prometheus) instead
of CPU alone, because search requests may block on I/O and keep CPU
low while requests queue. A CPU trigger is retained as a backstop.
"""

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

_PROMETHEUS_SERVER = "https://prometheus-prod-10-prod-us-central-0.grafana.net/api/prom"


def create_webapp_trigger_auth(
    env_name: str,
    namespace: str,
    k8s_global_labels: dict[str, str],
    stack_info: StackInfo,
    vault_k8s_resources: OLVaultK8SResources,
) -> tuple[kubernetes.apiextensions.CustomResource, str]:
    """Create the Prometheus TriggerAuthentication for webapp KEDA scaling.

    Returns:
        A tuple of (TriggerAuthentication resource, trigger authentication name).
    """
    auth_secret_name = f"{env_name}-mitlearn-webapp-prometheus-auth"
    trigger_auth_name = f"{env_name}-mitlearn-webapp-prometheus-auth-trigger"

    auth_secret = OLVaultK8SSecret(
        f"ol-{stack_info.env_prefix}-mitlearn-webapp-prometheus-auth-{stack_info.env_suffix}",
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
        f"ol-{stack_info.env_prefix}-mitlearn-webapp-prometheus-trigger-auth-{stack_info.env_suffix}",
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


def build_webapp_keda_config(
    trigger_auth_name: str,
    stack_info: StackInfo,
    mitlearn_config: pulumi.Config,
) -> OLApplicationK8sKedaWebappScalingConfig:
    """Build the KEDA ScaledObject config for the mit-learn webapp deployment.

    Aggregates all webapp APISIX routes (prefixed + no-prefix, passauth +
    reqauth) so total webapp traffic drives scaling, not a single route. The
    route label format is confirmed against live metrics:
    ``mitlearn_ol-mitlearn-k8s-apisix-route[-no-prefix]-<env_suffix>_<route_name>``.
    """
    route_regex = f"mitlearn_ol-mitlearn-k8s-apisix-route.*-{stack_info.env_suffix}_.*"

    requests_query = (
        f'sum(rate(apisix_http_status{{route=~"{route_regex}"}}[5m]))'
        f'/count(kube_pod_info{{job="integrations/kubernetes/kube-state-metrics",'
        f'namespace="mitlearn",pod=~"mitlearn-app-.*"}})'
    )
    requests_threshold = mitlearn_config.get("autoscaling_requests_threshold") or "20"

    latency_query = f'histogram_quantile(0.95,sum(rate(apisix_http_latency_bucket{{route=~"{route_regex}"}}[5m])) by (le))'
    latency_threshold = mitlearn_config.get("autoscaling_latency_threshold") or "2000"

    cpu_threshold = mitlearn_config.get("autoscaling_cpu_threshold") or "60"

    return OLApplicationK8sKedaWebappScalingConfig(
        scale_up_stabilization_seconds=60,
        scale_up_percent=50,
        scale_up_period_seconds=60,
        scale_down_stabilization_seconds=300 * 5,  # 25 minutes
        scale_down_percent=10,
        scale_down_period_seconds=300 * 5,  # 25 minutes
        polling_interval=60,
        cooldown_period=300,
        trigger_authentication_name=trigger_auth_name,
        triggers=[
            {
                "type": "prometheus",
                "metadata": {
                    "serverAddress": _PROMETHEUS_SERVER,
                    "query": requests_query,
                    "threshold": requests_threshold,
                    "authModes": "basic",
                },
            },
            {
                "type": "prometheus",
                "metadata": {
                    "serverAddress": _PROMETHEUS_SERVER,
                    "query": latency_query,
                    "threshold": latency_threshold,
                    "authModes": "basic",
                },
            },
            {
                "type": "cpu",
                "metricType": "Utilization",
                "metadata": {
                    "value": cpu_threshold,
                    "containerName": "mitlearn-app",
                },
            },
        ],
    )
