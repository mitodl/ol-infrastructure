"""KEDA autoscaling resources for the mit-learn webapp deployment.

Scales the webapp on APISIX request-rate and p95 latency (Prometheus) instead
of CPU alone, because search requests may block on I/O and keep CPU
low while requests queue. A CPU trigger is retained as a backstop.

The trigger-authentication and config-building logic lives in
``ol_infrastructure.lib.k8s_keda`` and is shared with the other webapps using
this pattern.
"""

import pulumi
import pulumi_kubernetes as kubernetes

from ol_infrastructure.components.services.k8s import (
    OLApplicationK8sKedaWebappScalingConfig,
)
from ol_infrastructure.components.services.vault import OLVaultK8SResources
from ol_infrastructure.lib.k8s_keda import (
    build_webapp_keda_config as _build_webapp_keda_config,
)
from ol_infrastructure.lib.k8s_keda import (
    create_webapp_prometheus_trigger_auth,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo


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
    return create_webapp_prometheus_trigger_auth(
        application_name="mitlearn",
        env_name=env_name,
        namespace=namespace,
        k8s_global_labels=k8s_global_labels,
        stack_info=stack_info,
        vault_k8s_resources=vault_k8s_resources,
    )


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
    return _build_webapp_keda_config(
        trigger_auth_name=trigger_auth_name,
        route_matcher=f"mitlearn_ol-mitlearn-k8s-apisix-route.*-{stack_info.env_suffix}_.*",
        namespace="mitlearn",
        pod_matcher="mitlearn-app-.*",
        container_name="mitlearn-app",
        requests_threshold=mitlearn_config.get("autoscaling_requests_threshold")
        or "20",
        latency_threshold=mitlearn_config.get("autoscaling_latency_threshold")
        or "2000",
        cpu_threshold=mitlearn_config.get("autoscaling_cpu_threshold") or "60",
    )
