# ruff: noqa: E501, PLR0913
from typing import Any

import pulumi
import pulumi_kubernetes as kubernetes
from pulumi import ResourceOptions

from bridge.lib.magic_numbers import DEFAULT_REDIS_PORT
from ol_infrastructure.components.aws.cache import OLAmazonCache
from ol_infrastructure.components.services.vault import (
    OLVaultK8SResources,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo


def create_autoscaling_resources(
    edxapp_cache: OLAmazonCache,
    edxapp_config: pulumi.Config,
    replicas_dict: dict[str, Any],
    namespace: str,
    k8s_global_labels: dict[str, str],
    lms_webapp_labels: dict[str, str],
    lms_celery_labels: dict[str, str],
    cms_webapp_labels: dict[str, str],
    cms_celery_labels: dict[str, str],
    lms_webapp_deployment_name: str,
    lms_celery_deployment_name: str,
    cms_webapp_deployment_name: str,
    cms_celery_deployment_name: str,
    stack_info: StackInfo,
    vault_k8s_resources: OLVaultK8SResources,
    lms_webapp_deployment: kubernetes.apps.v1.Deployment,
    lms_celery_deployment: kubernetes.apps.v1.Deployment,
    cms_webapp_deployment: kubernetes.apps.v1.Deployment,
    cms_celery_deployment: kubernetes.apps.v1.Deployment,
):
    """Create Kubernetes autoscaling resources (HPA, KEDA ScaledObjects).

    Args:
        edxapp_cache: OLAmazonCache instance for Redis connection
        replicas_dict: Dictionary containing replica count configurations
        namespace: Kubernetes namespace
        k8s_global_labels: Global labels to apply to resources
        lms_webapp_labels: Labels for LMS webapp resources
        lms_celery_labels: Labels for LMS celery resources
        cms_webapp_labels: Labels for CMS webapp resources
        cms_celery_labels: Labels for CMS celery resources
        lms_webapp_deployment_name: Name of LMS webapp deployment
        lms_celery_deployment_name: Name of LMS celery deployment
        cms_webapp_deployment_name: Name of CMS webapp deployment
        cms_celery_deployment_name: Name of CMS celery deployment
        stack_info: Stack information
        vault_k8s_resources: Vault Kubernetes resources for authentication
        lms_webapp_deployment: LMS webapp deployment resource
        lms_celery_deployment: LMS celery deployment resource
        cms_webapp_deployment: CMS webapp deployment resource
        cms_celery_deployment: CMS celery deployment resource

    Returns:
        Dictionary containing created autoscaling resources
    """
    env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

    # Create secret for Prometheus authentication from Vault
    webapp_prometheus_auth_secret_name = f"{env_name}-edxapp-webapp-prometheus-auth"
    webapp_prometheus_auth_secret = OLVaultK8SSecret(
        f"ol-{stack_info.env_prefix}-edxapp-webapp-prometheus-auth-{stack_info.env_suffix}",
        OLVaultK8SStaticSecretConfig(
            name=webapp_prometheus_auth_secret_name,
            namespace=namespace,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=webapp_prometheus_auth_secret_name,
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

    # Create TriggerAuthentication for Prometheus (shared between LMS and CMS)
    webapp_prometheus_trigger_auth = kubernetes.apiextensions.CustomResource(
        f"ol-{stack_info.env_prefix}-edxapp-webapp-prometheus-trigger-auth-{stack_info.env_suffix}",
        api_version="keda.sh/v1alpha1",
        kind="TriggerAuthentication",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{env_name}-edxapp-webapp-prometheus-auth-trigger",
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec={
            "secretTargetRef": [
                {
                    "parameter": "username",
                    "name": webapp_prometheus_auth_secret_name,
                    "key": "username",
                },
                {
                    "parameter": "password",
                    "name": webapp_prometheus_auth_secret_name,
                    "key": "password",
                },
            ]
        },
        opts=pulumi.ResourceOptions(depends_on=[webapp_prometheus_auth_secret]),
    )

    # Create KEDA ScaledObject for LMS deployment
    # This will scale the LMS deployment based on Prometheus metrics
    lms_prom_route_name = f"{stack_info.env_prefix}-openedx_ol-{stack_info.env_prefix}-edxapp-lms-apisix-route-{stack_info.env_suffix}_lms-default"

    # Requests / pod / second
    lms_requests_query = f"""
        sum(rate(apisix_http_status{{route="{lms_prom_route_name}"}}[5m]))
            /
        count(kube_pod_info{{job="integrations/kubernetes/kube-state-metrics", namespace="mitxonline-openedx", pod=~".*lms-webapp.*"}})
        """
    lms_requests_threshold = (
        edxapp_config.get("autoscaling_lms_requests_threshold") or "20"
    )  # 20 requests per pod per second

    # 95th percentile latency in milliseconds
    lms_latency_query = f'histogram_quantile(0.95,sum(rate(apisix_http_latency_bucket{{route="{lms_prom_route_name}"}}[5m])) by (le, route))'
    lms_latency_threshold = (
        edxapp_config.get("autoscaling_lms_latency_threshold") or "2000"
    )  # 2 seconds

    # CPU usage percentage
    lms_cpu_threshold = edxapp_config.get("autoscaling_lms_cpu_threshold") or "70"

    lms_webapp_scaledobject = kubernetes.apiextensions.CustomResource(
        f"ol-{stack_info.env_prefix}-edxapp-lms-scaledobject-{stack_info.env_suffix}",
        api_version="keda.sh/v1alpha1",
        kind="ScaledObject",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{lms_webapp_deployment_name}-scaledobject",
            namespace=namespace,
            labels=lms_webapp_labels,
        ),
        spec={
            "scaleTargetRef": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": lms_webapp_deployment_name,
            },
            "minReplicaCount": replicas_dict["webapp"]["lms"]["min"],
            "maxReplicaCount": replicas_dict["webapp"]["lms"]["max"],
            "pollingInterval": 60,  # check triggers every minute
            "cooldownPeriod": 300,  # wait 5 minutes before scaling down again
            "triggers": [
                # The primary trigger is based on requests per pod
                {
                    "type": "prometheus",
                    "metadata": {
                        "serverAddress": "https://prometheus-prod-10-prod-us-central-0.grafana.net/api/prom",
                        "query": lms_requests_query,
                        "threshold": lms_requests_threshold,
                        "authModes": "basic",
                    },
                    "authenticationRef": {
                        "name": f"{env_name}-edxapp-webapp-prometheus-auth-trigger",
                    },
                },
                # The secondary trigger is based on 95th percentile latency
                {
                    "type": "prometheus",
                    "metadata": {
                        "serverAddress": "https://prometheus-prod-10-prod-us-central-0.grafana.net/api/prom",
                        "query": lms_latency_query,
                        "threshold": lms_latency_threshold,
                        "authModes": "basic",
                    },
                    "authenticationRef": {
                        "name": f"{env_name}-edxapp-webapp-prometheus-auth-trigger",
                    },
                },
                # The tertiary trigger is based on CPU usage
                {
                    "type": "cpu",
                    "metricType": "AverageValue",
                    "metadata": {
                        "value": lms_cpu_threshold,
                        "containerName": "lms-edxapp",
                    },
                },
            ],
        },
        opts=pulumi.ResourceOptions(
            depends_on=[lms_webapp_deployment, webapp_prometheus_auth_secret]
        ),
    )

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

    # Create KEDA ScaledObject for CMS deployment
    # This will scale the CMS deployment based on Prometheus metrics
    cms_prom_route_name = f"{stack_info.env_prefix}-openedx_ol-{stack_info.env_prefix}-edxapp-cms-apisix-route-{stack_info.env_suffix}_cms-default"

    # Requests / pod / second
    cms_requests_query = f"""
        sum(rate(apisix_http_status{{route="{cms_prom_route_name}"}}[5m]))
            /
        count(kube_pod_info{{job="integrations/kubernetes/kube-state-metrics", namespace="mitxonline-openedx", pod=~".*cms-webapp.*"}})
        """
    cms_requests_threshold = (
        edxapp_config.get("autoscaling_cms_requests_threshold") or "20"
    )  # 20 requests per pod per second

    cms_cpu_threshold = edxapp_config.get("autoscaling_cms_cpu_threshold") or "70"

    cms_webapp_scaledobject = kubernetes.apiextensions.CustomResource(
        f"ol-{stack_info.env_prefix}-edxapp-cms-scaledobject-{stack_info.env_suffix}",
        api_version="keda.sh/v1alpha1",
        kind="ScaledObject",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{cms_webapp_deployment_name}-scaledobject",
            namespace=namespace,
            labels=cms_webapp_labels,
        ),
        spec={
            "scaleTargetRef": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": cms_webapp_deployment_name,
            },
            "minReplicaCount": replicas_dict["webapp"]["cms"]["min"],
            "maxReplicaCount": replicas_dict["webapp"]["cms"]["max"],
            "pollingInterval": 60,
            "cooldownPeriod": 300,
            "triggers": [
                # The primary trigger is based on requests per pod
                {
                    "type": "prometheus",
                    "metadata": {
                        "serverAddress": "https://prometheus-prod-10-prod-us-central-0.grafana.net/api/prom",
                        "query": cms_requests_query,
                        "threshold": cms_requests_threshold,
                        "authModes": "basic",
                    },
                    "authenticationRef": {
                        "name": f"{env_name}-edxapp-webapp-prometheus-auth-trigger",
                    },
                },
                # The secondary trigger is based on CPU usage
                {
                    "type": "cpu",
                    "metricType": "AverageValue",
                    "metadata": {
                        "value": cms_cpu_threshold,
                        "containerName": "cms-edxapp",
                    },
                },
            ],
        },
        opts=pulumi.ResourceOptions(
            depends_on=[cms_webapp_deployment, webapp_prometheus_auth_secret]
        ),
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
        "webapp_prometheus_auth_secret": webapp_prometheus_auth_secret,
        "webapp_prometheus_trigger_auth": webapp_prometheus_trigger_auth,
        "lms_webapp_scaledobject": lms_webapp_scaledobject,
        "lms_celery_scaledobject": lms_celery_scaledobject,
        "cms_webapp_scaledobject": cms_webapp_scaledobject,
        "cms_celery_scaledobject": cms_celery_scaledobject,
    }
