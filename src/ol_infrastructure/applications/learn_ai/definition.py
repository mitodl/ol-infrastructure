"""What learn-ai is in every environment it runs in.

The ``OLApplicationK8sConfig``, the gateway sessions and routes, and the canvas
consumer that do not depend on where the application runs live here.
Everything that does (the environment, hostnames, the Secrets to mount, the
image, sizing, what the cluster can do) arrives as a ``LearnAiBindings``. The
deployed program builds its bindings from the AWS and Vault resources it
creates. A local program builds them from the local platform. See
docs/plans/local-dev-deployed-parity.md.

Nothing in this module may read Pulumi config, a stack reference, a cloud API,
or ``os.environ``, so that importing it needs no provider.
"""

from pathlib import Path
from typing import Any, Literal

import pulumi_kubernetes as kubernetes
from pulumi import Output
from pydantic import BaseModel, ConfigDict, Field, field_validator

from bridge.lib.magic_numbers import STATIC_ASSET_MAX_AGE_SECONDS
from ol_infrastructure.components.services.k8s import (
    GranianConfig,
    OLApplicationK8sCeleryBeatConfig,
    OLApplicationK8sCeleryRedisConfig,
    OLApplicationK8sConfig,
    OLApplicationK8sKedaWebappScalingConfig,
)
from ol_infrastructure.lib.app_bindings import (
    ClusterCapabilities,
    WorkloadResources,
    celery_worker_configs,
    validate_queue_sizing,
)
from ol_infrastructure.lib.ol_types import KubernetesServiceAppProtocol
from ol_infrastructure.lib.pulumi_helper import merge_otel_resource_attributes

__all__ = [
    "API_PATH_PREFIX",
    "CANVAS_AGENT_CONSUMER_NAME",
    "CELERY_QUEUE_NAMES",
    "GRANIAN_WORKER_STARTUP_RSS",
    "LOCAL_AND_DEPLOYED_ENV",
    "NAMESPACE",
    "WEBAPP_SERVICE_NAME",
    "WEBAPP_SERVICE_PORT_NAME",
    "ClusterCapabilities",
    "LearnAiBindings",
    "LearnAiHostnames",
    "LearnAiSizing",
    "WorkloadResources",
    "application_config",
    "environment_variables",
]

NAMESPACE = "learn-ai"
WEBAPP_SERVICE_NAME = "learn-ai-webapp"
WEBAPP_SERVICE_PORT_NAME = "http"
# The MIT Learn API host serves this application under /ai/*.
API_PATH_PREFIX = "ai"
CANVAS_AGENT_CONSUMER_NAME = "canvas-agent"

# Environment values that hold wherever the application runs, a developer's
# cluster included; the bindings are applied over them. A value belongs here
# only if local-dev wants it too. Anything true of the deployed stacks alone
# stays in the stacks' learn_ai:env_vars or in the deployed program, not here.
LOCAL_AND_DEPLOYED_ENV: dict[str, Any] = {}

# The queues the deployed shape runs a worker for. Nothing routes to edx_content
# (learn-ai's task_routes is empty); its worker is part of the deployed shape.
# What each worker is given is a binding.
CELERY_QUEUE_NAMES = ["default", "edx_content"]

# Fresh pods in production sit at 523-595MiB container RSS (7d to 2026-10-15).
# On an RSS respawn the old worker keeps draining long-lived streamed responses
# beside the new one (up to ~22 minutes observed), so the cap has to leave room
# for both. All 3 OOMKills in the 14 days to 2026-10-15 came 65-90s into a
# respawn, two of them on pods admitted at a 1536Mi limit.
GRANIAN_WORKER_STARTUP_RSS = 600


class LearnAiHostnames(BaseModel):
    """Public hosts the application is served from."""

    model_config = ConfigDict(extra="forbid")

    api: str
    """The host the application is reached on directly."""
    frontend: str
    """The frontend's host; only ``cors_allow_origins`` reads it."""
    learn_api: str
    """MIT Learn's API host, which serves the application under ``/ai/*``."""


class LearnAiSizing(BaseModel):
    """Requests and limits per workload. The defaults are the deployed stacks'."""

    model_config = ConfigDict(extra="forbid")

    # Steady-state working set runs 600-630Mi, and main/apps.py imports litellm
    # at module load, so a shell or management command exec'd into a live pod
    # needs burst room above that. The request is the memory VPA's floor, and the
    # VPA keeps the request:limit ratio, so where the VPA runs this limit is the
    # lowest a pod is admitted with and sets the Granian RSS
    # cap: (0.9 * 2048 - GRANIAN_WORKER_STARTUP_RSS 600) = 1243MiB. That is close
    # to the 1382MiB cap 1536Mi gave without the reservation, so respawns stay
    # about as frequent (34 in 14 days). The p90 pod peaked at 1312MiB container
    # RSS over the 7 days to 2026-10-15.
    web: WorkloadResources = Field(
        default_factory=lambda: WorkloadResources(
            requests={"cpu": "100m", "memory": "1000Mi"}, limits={"memory": "2048Mi"}
        )
    )
    # learn-ai's main/apps.py imports litellm at module load (added in 0.36.2 to
    # fix a separate memory leak), which beat pays for at boot despite never
    # executing LLM code. 384Mi does not cover that baseline import cost and beat
    # OOMKilled on every startup. A request and limit of 1000Mi (zero memory
    # headroom) still showed ~25 beat restarts/week in production, so the limit
    # matches the celery workers'.
    celery_beat: WorkloadResources = Field(
        default_factory=lambda: WorkloadResources(
            requests={"cpu": "10m", "memory": "1000Mi"}, limits={"memory": "1536Mi"}
        )
    )
    # Each worker runs --concurrency=2 prefork (set by the component), so each
    # forked child re-pays the litellm import cost: the steady-state working set
    # sat at 815-930Mi against a 1000Mi limit, and one queue was OOMKilled in
    # production. The limit gives the prefork children room without changing the
    # pool model.
    celery_queues: dict[str, WorkloadResources] = Field(
        validate_default=True,
        default_factory=lambda: {
            name: WorkloadResources(
                requests={"cpu": "100m", "memory": "1000Mi"},
                limits={"memory": "1536Mi"},
            )
            for name in CELERY_QUEUE_NAMES
        },
    )

    @field_validator("celery_queues")
    @classmethod
    def _every_queue_once(
        cls, queues: dict[str, WorkloadResources]
    ) -> dict[str, WorkloadResources]:
        return validate_queue_sizing(queues, CELERY_QUEUE_NAMES)


class LearnAiBindings(BaseModel):
    """Everything about learn-ai that differs between environments."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    env_suffix: str
    hostnames: LearnAiHostnames
    environment_variables: dict[str, Any]
    """This environment's values, applied over ``LOCAL_AND_DEPLOYED_ENV``."""
    cors_frontend_origins: list[str]
    k8s_labels: dict[str, str]
    secret_names: list[str]
    optional_secret_names: list[str] = Field(default_factory=list)
    """Secrets the pods mount if they exist and start without if they do not."""
    oidc_secret_name: str | None = None
    application_docker_tag: str | None = None
    application_image_digest: str | None = None
    application_version: str | None = None
    """The release the image was built for; labels the pods. The release
    pipeline sets it alongside the digest."""
    cluster: ClusterCapabilities
    service_account_name: str | Output[str] | None = None
    # The deployed stacks pull the published image; a cluster that serves the
    # app from its own registry names the repository here.
    image_repository: str = "mitodl/learn-ai-app"
    min_replicas: int
    sizing: LearnAiSizing = Field(default_factory=LearnAiSizing)
    celery_topology: Literal["per-queue", "merged"] = "per-queue"
    """``merged`` runs one worker Deployment consuming every queue, for clusters
    where a Deployment per queue costs more than it tells a developer."""
    celery_redis: OLApplicationK8sCeleryRedisConfig | None = None
    webapp_keda: OLApplicationK8sKedaWebappScalingConfig | None = None
    slack_channel: str | None = None
    extra_volumes: list[kubernetes.core.v1.VolumeArgs] = Field(default_factory=list)
    extra_volume_mounts: list[kubernetes.core.v1.VolumeMountArgs] = Field(
        default_factory=list
    )


def environment_variables(bindings: LearnAiBindings) -> dict[str, Any]:
    """Return the application's environment for these bindings.

    :param bindings: The environment being rendered for.
    :returns: ``LOCAL_AND_DEPLOYED_ENV`` under the bindings' values, with the k8s
        labels folded into ``OTEL_RESOURCE_ATTRIBUTES``.
    :rtype: dict[str, Any]
    """
    env_vars = {**LOCAL_AND_DEPLOYED_ENV, **bindings.environment_variables}
    merge_otel_resource_attributes(env_vars, bindings.k8s_labels)
    return env_vars


def application_config(bindings: LearnAiBindings) -> OLApplicationK8sConfig:
    """Return the ``OLApplicationK8s`` config for these bindings.

    :param bindings: The environment being rendered for.
    :returns: The config to hand to ``OLApplicationK8s``.
    :rtype: OLApplicationK8sConfig
    """
    cluster = bindings.cluster
    sizing = bindings.sizing
    return OLApplicationK8sConfig(
        project_root=Path(__file__).parent,
        application_config=environment_variables(bindings),
        application_name="learn-ai",
        application_namespace=NAMESPACE,
        application_lb_service_name=WEBAPP_SERVICE_NAME,
        application_lb_service_port_name=WEBAPP_SERVICE_PORT_NAME,
        application_lb_service_app_protocol=KubernetesServiceAppProtocol.WS,
        k8s_global_labels=bindings.k8s_labels,
        env_from_secret_names=bindings.secret_names,
        optional_env_from_secret_names=bindings.optional_secret_names,
        application_security_group_id=cluster.security_group_id,
        application_security_group_name=cluster.security_group_name,
        application_service_account_name=bindings.service_account_name,
        extra_volumes=bindings.extra_volumes,
        extra_volume_mounts=bindings.extra_volume_mounts,
        application_image_repository=bindings.image_repository,
        application_docker_tag=bindings.application_docker_tag,
        application_image_digest=bindings.application_image_digest,
        application_version=bindings.application_version,
        application_min_replicas=bindings.min_replicas,
        granian_config=GranianConfig(
            interface="asgi",
            workers=1,
            # Above the measured cold start: RSS respawns in production took
            # 14.7-19.5s from "Spawning worker-1" to "Started worker-1"
            # (2026-10-14). Granian 2.8 stops the old worker once this interval
            # elapses whether or not the new one is listening, so the 3.5s default
            # would refuse connections for ~15s per respawn (see
            # GranianConfig.respawn_interval).
            respawn_interval=60,
            worker_startup_rss=GRANIAN_WORKER_STARTUP_RSS,
            runtime_mode=None,
            # 2, not the component default of 1: a holding value until the Granian
            # overhaul's async-apps stage sizes learn_ai
            # (docs/plans/granian-configuration-overhaul.md). asgi forces
            # blocking_threads=1, so this is the only Granian thread setting that
            # applies.
            runtime_threads=2,
            no_ws=False,
            backlog=None,
            log_level="debug",
            application_module="main.asgi:application",
            enable_metrics=True,
            # Serve /static/* from Granian's Rust layer, so the nginx sidecar
            # (import_nginx_config below) is not needed
            # (docs/plans/remove-nginx-sidecar.md, stage 4), matching the
            # STATIC_ROOT/STATIC_URL precedent from ocw_studio/xpro.
            static_path_mounts=["/src/staticfiles"],
            static_path_expires=STATIC_ASSET_MAX_AGE_SECONDS,
        ),
        slack_channel=bindings.slack_channel,
        vault_k8s_resource_auth_name=cluster.vault_auth_name,
        registry=cluster.registry,
        import_nginx_config=False,
        resource_requests=sizing.web.requests,
        resource_limits=sizing.web.limits,
        init_migrations=True,
        # Nothing here runs createcachetable. learn-ai's durable and
        # external_assets caches are DatabaseCache, so a database that never had
        # the Heroku release phase run against it needs the tables created out of
        # band.
        init_collectstatic=True,
        celery_redis_config=bindings.celery_redis,
        celery_worker_configs=celery_worker_configs(
            CELERY_QUEUE_NAMES, bindings.celery_topology, sizing.celery_queues
        ),
        celery_beat_config=OLApplicationK8sCeleryBeatConfig(
            scheduler="celery.beat.PersistentScheduler",
            resource_requests=sizing.celery_beat.requests,
            resource_limits=sizing.celery_beat.limits,
        ),
        # hpa_scaling_metrics stays at the component default; it applies only where
        # autoscalers are on and webapp_keda is unbound. The deployed program binds
        # webapp_keda, whose triggers carry the CPU backstop, and the memory VPA
        # manages memory.
        webapp_keda_config=bindings.webapp_keda,
        manage_webapp_autoscaler=cluster.autoscalers,
        manage_celery_autoscalers=cluster.autoscalers,
        manage_webapp_memory_vpa=cluster.autoscalers,
        manage_pod_monitor=cluster.pod_monitors,
    )
