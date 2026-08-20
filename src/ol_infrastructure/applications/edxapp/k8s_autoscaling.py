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
    MODEL_DEFAULT_SCALE_DOWN_SECONDS,
    build_webapp_keda_config,
    create_webapp_prometheus_trigger_auth,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo

_MEMORY_UNIT_MULTIPLIERS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}


def _memory_quantity_fraction_bytes(quantity: str, fraction: float) -> str:
    """Convert a k8s memory quantity string (e.g. "4Gi") to a byte count at
    the given fraction of it, as a plain decimal string.

    Used to give a KEDA memory trigger an AverageValue target that is fixed
    at deploy time from the declared memory_request, rather than a
    Utilization target computed live against whatever a VPA has resized that
    request to -- see the comment above the CMS celery ScaledObject for why
    that distinction matters here.
    """
    for suffix, multiplier in _MEMORY_UNIT_MULTIPLIERS.items():
        if quantity.endswith(suffix):
            value = float(quantity[: -len(suffix)]) * multiplier
            break
    else:
        value = float(quantity)  # already plain bytes
    return str(int(value * fraction))


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

    The request-rate trigger's per-pod divisor is gone entirely -- the HPA
    already divides by replica count for an AverageValue metric, so the explicit
    divisor double-divided and left the trigger inert. See the metric-type notes
    in ol_infrastructure.lib.k8s_keda. This also retires the earlier bug where
    that divisor's namespace was hardcoded to "mitxonline-openedx" for every
    edxapp deployment, making the mitx, xpro and mitx-staging stacks divide their
    own request rate by mitxonline's pod count.

    No latency trigger either, for a different reason than CMS below: LMS p95 is
    dominated by MySQL, MongoDB and Redis, so a latency excursion is usually a
    signal about a shared backend rather than about LMS capacity. Adding LMS
    replicas then makes it worse -- more pods means more connections against the
    component that is already the constraint -- while the trigger keeps reading
    high latency and keeps asking for more, up to max_replicas. Request rate plus
    the CPU backstop covers the cases where replicas actually help.

    This matters more now that the trigger is emitted as ``Value`` rather than
    ``AverageValue``: as ``AverageValue`` it was inert (it needed a 60s p95 to
    reach a 30-replica ceiling), whereas ``Value`` turns a routine 4-6s incident
    excursion into a demand for 2-3x the current replica count. Set
    ``autoscaling_lms_latency_threshold`` on a stack to opt back in; a value
    around 4000 keeps the signal with more headroom than the old 2000 default,
    against observed p95 of 100-953ms across the production LMS stacks.
    """
    return build_webapp_keda_config(
        trigger_auth_name=trigger_auth_name,
        route_matcher=f"{stack_info.env_prefix}-openedx_ol-{stack_info.env_prefix}-edxapp-lms-apisix-route-{stack_info.env_suffix}_lms-default",
        container_name="lms-edxapp-app",
        requests_threshold=edxapp_config.get("autoscaling_lms_requests_threshold")
        or "20",
        latency_threshold=edxapp_config.get("autoscaling_lms_latency_threshold"),
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
    saturation signal there.

    CMS also keeps the 5-minute scale-down it had before adopting the shared
    helper, rather than inheriting the helper's 25-minute default. Authoring
    traffic is bursty and comparatively low-volume, so holding replicas for 25
    minutes after a burst costs capacity without buying much. Making this
    explicit keeps the refactor behaviour-preserving for CMS.

    See build_lms_webapp_keda_config for the request-rate divisor removal.
    """
    return build_webapp_keda_config(
        trigger_auth_name=trigger_auth_name,
        route_matcher=f"{stack_info.env_prefix}-openedx_ol-{stack_info.env_prefix}-edxapp-cms-apisix-route-{stack_info.env_suffix}_cms-default",
        container_name="cms-edxapp-app",
        requests_threshold=edxapp_config.get("autoscaling_cms_requests_threshold")
        or "20",
        latency_threshold=None,
        cpu_threshold=edxapp_config.get("autoscaling_cms_cpu_threshold") or "70",
        scale_down_stabilization_seconds=MODEL_DEFAULT_SCALE_DOWN_SECONDS,
        scale_down_period_seconds=MODEL_DEFAULT_SCALE_DOWN_SECONDS,
    )


def create_celery_autoscaling_resources(
    edxapp_cache: OLAmazonCache,
    replicas_dict: dict[str, Any],
    cms_celery_memory_request: str,
    namespace: str,
    lms_celery_labels: dict[str, str],
    lms_high_mem_celery_labels: dict[str, str],
    cms_celery_labels: dict[str, str],
    lms_celery_deployment_name: str,
    lms_high_mem_celery_deployment_name: str,
    cms_celery_deployment_name: str,
    stack_info: StackInfo,
    lms_celery_deployment: kubernetes.apps.v1.Deployment,
    lms_high_mem_celery_deployment: kubernetes.apps.v1.Deployment,
    cms_celery_deployment: kubernetes.apps.v1.Deployment,
) -> dict[str, Any]:
    """Create KEDA ScaledObjects for the celery worker deployments.

    Covers three workers: the shared LMS worker (edx.lms.core.default), the
    dedicated LMS high_mem worker that serves the long instructor-task reports
    (edx.lms.core.high_mem), and the CMS worker (edx.cms.core.default).

    Celery workers use Redis list length triggers (not Prometheus) so they
    remain outside the OLApplicationK8s component. CMS also carries an
    additional memory trigger -- see the comment above the CMS ScaledObject
    below for why queue depth alone missed a real slowdown, and why that
    trigger targets an absolute AverageValue rather than Utilization.

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

    # Nothing previously watched edx.lms.core.high_mem, so a queued grade report
    # produced no scale-up signal at all. Scale on that queue directly.
    #
    # listLength is 1: these tasks are minutes-to-hours long and singly-concurrent per
    # pod, so any queued message means another worker is warranted -- unlike the
    # default queue where a backlog of 10 short tasks is normal.
    #
    # The scale-down guards matter more here than the scale-up ones. A task that has
    # been picked up no longer counts toward list length, so a lone in-flight report
    # leaves the queue reading empty; without these the HPA would immediately scale
    # back toward minReplicaCount and delete the pod running it. The long grace period
    # on the deployment means such a pod still finishes its task, but there is no
    # reason to provoke that repeatedly.
    high_mem_replicas = replicas_dict["celery"].get(
        "lms_high_mem", {"min": 1, "max": 3}
    )
    lms_high_mem_celery_scaledobject = kubernetes.apiextensions.CustomResource(
        f"ol-{stack_info.env_prefix}-edxapp-lms-high-mem-celery-scaledobject-{stack_info.env_suffix}",
        api_version="keda.sh/v1alpha1",
        kind="ScaledObject",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{lms_high_mem_celery_deployment_name}-scaledobject",
            namespace=namespace,
            labels=lms_high_mem_celery_labels,
        ),
        spec={
            "scaleTargetRef": {
                "kind": "Deployment",
                "name": lms_high_mem_celery_deployment_name,
            },
            "pollingInterval": 60,
            "cooldownPeriod": 1800,
            "minReplicaCount": high_mem_replicas["min"],
            "maxReplicaCount": high_mem_replicas["max"],
            "advanced": {
                "horizontalPodAutoscalerConfig": {
                    "behavior": {
                        "scaleUp": {"stabilizationWindowSeconds": 60},
                        "scaleDown": {
                            "stabilizationWindowSeconds": 3600,
                            "policies": [
                                {"type": "Pods", "value": 1, "periodSeconds": 1800},
                            ],
                        },
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
                        "listName": "edx.lms.core.high_mem",
                        "listLength": "1",
                        "enableTLS": "true",
                    },
                }
            ],
        },
        opts=pulumi.ResourceOptions(depends_on=[lms_high_mem_celery_deployment]),
    )

    # This was the only celery ScaledObject with no behavior block, so it ran on the
    # HPA defaults: a 300s scale-down window and an unbounded scale-up that doubles
    # replicas every 15s. A bulk course publish drove the observed sequence
    # 1 -> 4 -> 8 -> 16 -> 20 -> 1 -> 20 inside a couple of hours.
    #
    # Every one of those cycles is expensive out of proportion to the work it absorbs.
    # The edxapp image is ~2GB and a cold pull was measured at 3m56s, so a pod
    # summoned by a burst routinely becomes ready after the burst it was meant to
    # serve has already drained. Karpenter then scales the node back out from under
    # it -- one pod was evicted as "Underutilized" 8 minutes after being created.
    #
    # scaleUp at 300s: match the shared LMS worker. A CMS publish burst that is still
    # queued five minutes later is a real backlog; anything shorter is absorbed by the
    # replicas already running and would only buy an image pull that lands too late.
    #
    # scaleDown at 1800s with a 1-pod/300s policy: bleed replicas off gradually rather
    # than dropping 20 -> 1 the moment the queue reads empty. Queue depth goes to zero
    # as soon as tasks are picked up, not when they finish, so an empty
    # edx.cms.core.default is a poor signal that the work is done -- and the git export
    # tasks that dominate these bursts run 30-113s each. Bleeding down also keeps warm
    # pods around for the next burst, which is what actually breaks the flapping cycle.
    #
    # Second trigger: memory. On 2026-08-19 a mitxonline production cms-celery
    # replica's memory climbed from ~1.3Gi to ~3.5Gi (of a 4Gi request / 6Gi
    # VPA-managed limit) over about 17 hours -- driven by a full-catalog search
    # reindex sharing the worker with routine CMS tasks -- while course exports
    # got steadily slower. The HPA's desired-replica count never left 1 the
    # entire time: tasks were still being dequeued promptly, so
    # edx.cms.core.default never crossed listLength=10. A queue-depth trigger
    # can't see "the worker that already grabbed the task is now
    # resource-starved and slow to finish it" -- it only sees an empty list.
    #
    # This uses metricType AverageValue against an absolute byte target
    # (_memory_quantity_fraction_bytes(cms_celery_memory_request, 0.7)), not
    # Utilization, deliberately: the CMS celery VPA a few resources down
    # (k8s_resources.py, controlled_resources=["cpu", "memory"]) resizes this
    # same container's memory request in place. A Utilization trigger's target
    # is a percentage of that same live request, so every VPA resize would also
    # move the HPA's threshold -- the two autoscalers fighting over one signal,
    # the exact failure mode the webapp path avoids for cpu (see the VPA
    # comment above _worker_vpa_min_allowed in k8s_resources.py). AverageValue
    # is computed once from the config value at deploy time and does not move
    # when the VPA resizes the live request, so it stays a stable, independent
    # signal. 70% of the configured request gives a few hours of lead time at
    # the observed climb rate, comfortably before the memory_limit ceiling --
    # scaled automatically per stack since each stack configures its own
    # memory_request (1-4Gi across the mitx/mitxonline/xpro/mitx-staging
    # stacks). The HPA takes the max desired-replica count across all triggers,
    # so this is additive to the redis trigger above, not a replacement --
    # either signal firing scales the deployment up. Deliberately rolled out to
    # every stack using this shared function, not gated to mitxonline
    # production alone: the underlying gap (queue depth can't see a
    # resource-starved worker) is structural to this ScaledObject shape, not
    # specific to the incident that surfaced it.
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
            "advanced": {
                "horizontalPodAutoscalerConfig": {
                    "behavior": {
                        "scaleUp": {"stabilizationWindowSeconds": 300},
                        "scaleDown": {
                            "stabilizationWindowSeconds": 1800,
                            "policies": [
                                {"type": "Pods", "value": 1, "periodSeconds": 300},
                            ],
                        },
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
                        "listName": "edx.cms.core.default",
                        "listLength": "10",
                        "enableTLS": "true",
                    },
                },
                {
                    "type": "memory",
                    "metricType": "AverageValue",
                    "metadata": {
                        "value": _memory_quantity_fraction_bytes(
                            cms_celery_memory_request, 0.7
                        ),
                    },
                },
            ],
        },
        opts=pulumi.ResourceOptions(depends_on=[cms_celery_deployment]),
    )

    return {
        "lms_celery_scaledobject": lms_celery_scaledobject,
        "lms_high_mem_celery_scaledobject": lms_high_mem_celery_scaledobject,
        "cms_celery_scaledobject": cms_celery_scaledobject,
    }
