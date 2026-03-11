# ruff: noqa: E501, PLR0913
"""KEDA autoscaling resources for the edxapp application."""

from typing import Any

import pulumi
import pulumi_kubernetes as kubernetes
from pulumi import ResourceOptions

from bridge.lib.magic_numbers import DEFAULT_REDIS_PORT
from ol_infrastructure.components.aws.cache import OLAmazonCache
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

    This resource is shared between LMS and CMS ScaledObjects and must be
    created before the OLApplicationK8s component instances so the trigger
    authentication name is available for the webapp_keda_config.

    Returns:
        A tuple of (TriggerAuthentication resource, trigger authentication name).
    """
    auth_secret_name = f"{env_name}-edxapp-webapp-prometheus-auth"
    trigger_auth_name = f"{env_name}-edxapp-webapp-prometheus-auth-trigger"

    auth_secret = OLVaultK8SSecret(
        f"ol-{stack_info.env_prefix}-edxapp-webapp-prometheus-auth-{stack_info.env_suffix}",
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
        f"ol-{stack_info.env_prefix}-edxapp-webapp-prometheus-trigger-auth-{stack_info.env_suffix}",
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


def build_lms_webapp_keda_config(
    trigger_auth_name: str,
    stack_info: StackInfo,
    edxapp_config: pulumi.Config,
) -> OLApplicationK8sKedaWebappScalingConfig:
    """Build the KEDA ScaledObject config for the LMS webapp deployment."""
    lms_prom_route_name = f"{stack_info.env_prefix}-openedx_ol-{stack_info.env_prefix}-edxapp-lms-apisix-route-{stack_info.env_suffix}_lms-default"

    lms_requests_query = f'sum(rate(apisix_http_status{{route="{lms_prom_route_name}"}}[5m]))/count(kube_pod_info{{job="integrations/kubernetes/kube-state-metrics",namespace="mitxonline-openedx",pod=~".*lms-edxapp-app.*"}})'
    lms_requests_threshold = (
        edxapp_config.get("autoscaling_lms_requests_threshold") or "20"
    )

    lms_latency_query = f'histogram_quantile(0.95,sum(rate(apisix_http_latency_bucket{{route="{lms_prom_route_name}"}}[5m])) by (le, route))'
    lms_latency_threshold = (
        edxapp_config.get("autoscaling_lms_latency_threshold") or "2000"
    )

    lms_cpu_threshold = edxapp_config.get("autoscaling_lms_cpu_threshold") or "70"

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
                    "query": lms_requests_query,
                    "threshold": lms_requests_threshold,
                    "authModes": "basic",
                },
            },
            {
                "type": "prometheus",
                "metadata": {
                    "serverAddress": _PROMETHEUS_SERVER,
                    "query": lms_latency_query,
                    "threshold": lms_latency_threshold,
                    "authModes": "basic",
                },
            },
            {
                "type": "cpu",
                "metricType": "AverageValue",
                "metadata": {
                    "value": lms_cpu_threshold,
                    "containerName": "lms-edxapp-app",
                },
            },
        ],
    )


def build_cms_webapp_keda_config(
    trigger_auth_name: str,
    stack_info: StackInfo,
    edxapp_config: pulumi.Config,
) -> OLApplicationK8sKedaWebappScalingConfig:
    """Build the KEDA ScaledObject config for the CMS webapp deployment."""
    cms_prom_route_name = f"{stack_info.env_prefix}-openedx_ol-{stack_info.env_prefix}-edxapp-cms-apisix-route-{stack_info.env_suffix}_cms-default"

    cms_requests_query = f'sum(rate(apisix_http_status{{route="{cms_prom_route_name}"}}[5m]))/count(kube_pod_info{{job="integrations/kubernetes/kube-state-metrics",namespace="mitxonline-openedx",pod=~".*cms-edxapp-app.*"}})'
    cms_requests_threshold = (
        edxapp_config.get("autoscaling_cms_requests_threshold") or "20"
    )

    cms_cpu_threshold = edxapp_config.get("autoscaling_cms_cpu_threshold") or "70"

    return OLApplicationK8sKedaWebappScalingConfig(
        polling_interval=60,
        cooldown_period=300,
        trigger_authentication_name=trigger_auth_name,
        triggers=[
            {
                "type": "prometheus",
                "metadata": {
                    "serverAddress": _PROMETHEUS_SERVER,
                    "query": cms_requests_query,
                    "threshold": cms_requests_threshold,
                    "authModes": "basic",
                },
            },
            {
                "type": "cpu",
                "metricType": "AverageValue",
                "metadata": {
                    "value": cms_cpu_threshold,
                    "containerName": "cms-edxapp-app",
                },
            },
        ],
    )


def create_celery_autoscaling_resources(
    edxapp_cache: OLAmazonCache,
    replicas_dict: dict[str, Any],
    namespace: str,
    lms_celery_labels: dict[str, str],
    cms_celery_labels: dict[str, str],
    lms_celery_deployment_name: str,
    cms_celery_deployment_name: str,
    stack_info: StackInfo,
    lms_celery_deployment: kubernetes.apps.v1.Deployment,
    cms_celery_deployment: kubernetes.apps.v1.Deployment,
) -> dict[str, Any]:
    """Create KEDA ScaledObjects for LMS and CMS celery worker deployments.

    Celery workers use Redis list length triggers (not Prometheus) so they
    remain outside the OLApplicationK8s component.

    Returns:
        Dictionary containing created celery autoscaling resources.
    """
    lms_celery_scaledobject = kubernetes.apiextensions.CustomResource(
        f"ol-{stack_info.env_prefix}-edxapp-lms-celery-scaledobject-{stack_info.env_suffix}",
        api_version="keda.sh/v1alpha1",
        kind="ScaledObject",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{lms_celery_deployment_name}-scaledobject",
            namespace=namespace,
            labels=lms_celery_labels,
        ),
        spec={
            "scaleTargetRef": {
                "kind": "Deployment",
                "name": lms_celery_deployment_name,
            },
            "pollingInterval": 60,
            "cooldownPeriod": 300,
            "minReplicaCount": replicas_dict["celery"]["lms"]["min"],
            "maxReplicaCount": replicas_dict["celery"]["lms"]["max"],
            "advanced": {
                "horizontalPodAutoscalerConfig": {
                    "behavior": {
                        "scaleUp": {"stabilizationWindowSeconds": 300},
                    }
                }
            },
            "triggers": [
                {
                    "type": "redis",
                    "metadata": {
                        "address": edxapp_cache.address.apply(
                            lambda addr: f"{addr}:{DEFAULT_REDIS_PORT}"
                        ),
                        "username": "default",
                        "databaseIndex": "1",
                        "password": edxapp_cache.cache_cluster.auth_token,
                        "listName": "edx.lms.core.default",
                        "listLength": "10",
                        "enableTLS": "true",
                    },
                }
            ],
        },
        opts=pulumi.ResourceOptions(depends_on=[lms_celery_deployment]),
    )

    cms_celery_scaledobject = kubernetes.apiextensions.CustomResource(
        f"ol-{stack_info.env_prefix}-edxapp-cms-celery-scaledobject-{stack_info.env_suffix}",
        api_version="keda.sh/v1alpha1",
        kind="ScaledObject",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{cms_celery_deployment_name}-scaledobject",
            namespace=namespace,
            labels=cms_celery_labels,
        ),
        spec={
            "scaleTargetRef": {
                "kind": "Deployment",
                "name": cms_celery_deployment_name,
            },
            "pollingInterval": 60,
            "cooldownPeriod": 300,
            "minReplicaCount": replicas_dict["celery"]["cms"]["min"],
            "maxReplicaCount": replicas_dict["celery"]["cms"]["max"],
            "triggers": [
                {
                    "type": "redis",
                    "metadata": {
                        "address": edxapp_cache.address.apply(
                            lambda addr: f"{addr}:{DEFAULT_REDIS_PORT}"
                        ),
                        "username": "default",
                        "databaseIndex": "1",
                        "password": edxapp_cache.cache_cluster.auth_token,
                        "listName": "edx.cms.core.default",
                        "listLength": "10",
                        "enableTLS": "true",
                    },
                }
            ],
        },
        opts=pulumi.ResourceOptions(depends_on=[cms_celery_deployment]),
    )

    return {
        "lms_celery_scaledobject": lms_celery_scaledobject,
        "cms_celery_scaledobject": cms_celery_scaledobject,
    }
