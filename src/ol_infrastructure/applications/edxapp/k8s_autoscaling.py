# ruff: noqa: PLR0913
"""KEDA autoscaling resources for the edxapp application."""

from typing import Any

import pulumi
import pulumi_kubernetes as kubernetes

from bridge.lib.magic_numbers import DEFAULT_REDIS_PORT
from ol_infrastructure.components.aws.cache import OLAmazonCache
from ol_infrastructure.components.services.k8s import (
    OLApplicationK8sKedaWebappScalingConfig,
)
from ol_infrastructure.components.services.vault import OLVaultK8SResources
from ol_infrastructure.lib.k8s_keda import (
    build_webapp_keda_config,
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

    This resource is shared between LMS and CMS ScaledObjects and must be
    created before the OLApplicationK8s component instances so the trigger
    authentication name is available for the webapp_keda_config.

    Returns:
        A tuple of (TriggerAuthentication resource, trigger authentication name).
    """
    return create_webapp_prometheus_trigger_auth(
        application_name="edxapp",
        env_name=env_name,
        namespace=namespace,
        k8s_global_labels=k8s_global_labels,
        stack_info=stack_info,
        vault_k8s_resources=vault_k8s_resources,
    )


def build_lms_webapp_keda_config(
    trigger_auth_name: str,
    stack_info: StackInfo,
    edxapp_config: pulumi.Config,
) -> OLApplicationK8sKedaWebappScalingConfig:
    """Build the KEDA ScaledObject config for the LMS webapp deployment.

    NOTE: the per-pod divisor namespace is now derived from the stack's
    env_prefix. It was previously hardcoded to "mitxonline-openedx" for every
    edxapp deployment, so the mitx, xpro and mitx-staging stacks were dividing
    their own request rate by mitxonline's pod count.
    """
    return build_webapp_keda_config(
        trigger_auth_name=trigger_auth_name,
        route_matcher=f"{stack_info.env_prefix}-openedx_ol-{stack_info.env_prefix}-edxapp-lms-apisix-route-{stack_info.env_suffix}_lms-default",
        namespace=f"{stack_info.env_prefix}-openedx",
        pod_matcher=".*lms-edxapp-app.*",
        container_name="lms-edxapp-app",
        requests_threshold=edxapp_config.get("autoscaling_lms_requests_threshold")
        or "20",
        latency_threshold=edxapp_config.get("autoscaling_lms_latency_threshold")
        or "2000",
        cpu_threshold=edxapp_config.get("autoscaling_lms_cpu_threshold") or "70",
    )


def build_cms_webapp_keda_config(
    trigger_auth_name: str,
    stack_info: StackInfo,
    edxapp_config: pulumi.Config,
) -> OLApplicationK8sKedaWebappScalingConfig:
    """Build the KEDA ScaledObject config for the CMS webapp deployment.

    No latency trigger: CMS request duration is dominated by course-import and
    asset-upload payload size rather than by contention, so p95 latency is not a
    saturation signal there. This preserves the pre-existing behaviour.

    See build_lms_webapp_keda_config for the divisor-namespace correction.
    """
    return build_webapp_keda_config(
        trigger_auth_name=trigger_auth_name,
        route_matcher=f"{stack_info.env_prefix}-openedx_ol-{stack_info.env_prefix}-edxapp-cms-apisix-route-{stack_info.env_suffix}_cms-default",
        namespace=f"{stack_info.env_prefix}-openedx",
        pod_matcher=".*cms-edxapp-app.*",
        container_name="cms-edxapp-app",
        requests_threshold=edxapp_config.get("autoscaling_cms_requests_threshold")
        or "20",
        latency_threshold=None,
        cpu_threshold=edxapp_config.get("autoscaling_cms_cpu_threshold") or "70",
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
