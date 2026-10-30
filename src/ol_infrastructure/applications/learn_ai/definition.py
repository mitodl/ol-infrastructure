"""What learn-ai is in every environment it runs in.

Everything that differs between environments arrives as a ``LearnAiBindings``;
see docs/plans/local-dev-deployed-parity.md.

Nothing in this module may read Pulumi config, a stack reference, a cloud API,
or ``os.environ``, so that importing it needs no provider.
"""

from pathlib import Path
from typing import Any, Literal

import pulumi_kubernetes as kubernetes
from pulumi import Input, Output
from pydantic import BaseModel, ConfigDict, Field, field_validator

from bridge.lib.constants import LearnSession, apisix_oidc_session_cookie_name
from bridge.lib.magic_numbers import STATIC_ASSET_MAX_AGE_SECONDS
from ol_infrastructure.components.services.apisix import (
    FORWARDED_ACCESS_TOKEN_EXPIRES_LEEWAY_SECONDS,
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
    OLApisixOIDCRouteGroup,
    OLApisixOIDCSecretCredentials,
    OLApisixOIDCVaultCredentials,
    OLApisixPluginConfig,
    OLApisixRouteConfig,
    learn_session_oidc_config,
    stale_session_cookie_cleanup_plugin,
)
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
    oidc_credentials,
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
    "canvas_agent_consumer_spec",
    "cors_allow_origins",
    "direct_oidc_config",
    "direct_route_configs",
    "environment_variables",
    "learn_session",
    "prefixed_oidc_config",
    "prefixed_route_configs",
    "prefixed_route_group",
    "proxy_rewrite_plugin",
    "shared_plugins",
]

NAMESPACE = "learn-ai"
WEBAPP_SERVICE_NAME = "learn-ai-webapp"
WEBAPP_SERVICE_PORT_NAME = "http"
# The MIT Learn API host serves this application under /ai/*.
API_PATH_PREFIX = "ai"
CANVAS_AGENT_CONSUMER_NAME = "canvas-agent"

# Values local-dev wants as much as the deployed stacks do. A value only the
# deployed stacks want belongs in their learn_ai:env_vars or the deployed program.
LOCAL_AND_DEPLOYED_ENV: dict[str, Any] = {
    # The demo courses every environment allows. These are lists because
    # OLApplicationK8sConfig renders every env value with str(), so a list
    # reaches the pod as its Python repr, which learn-ai's main/envs.py parses
    # with literal_eval. A JSON string here would reach the pod as a different
    # value.
    "CANVAS_SYLLABUS_DEMO_READABLE_IDS": ["14566-kaleba:20211202", "34642-15.095_FA25"],
    "CANVAS_TUTOR_DEMO_RUN_READABLE_IDS": [
        "14566-kaleba:20211202+canvas",
        "34642-15.095_FA25+canvas",
    ],
    # The published image has no debug_toolbar, so a true value crashes it at
    # startup. Only the local-dev image built from a learn-ai checkout can run
    # with DEBUG on, and a developer sets that per cluster.
    "DEBUG": "false",
    # Feature flags default on where no PostHog key decides them.
    "MITOL_FEATURES_DEFAULT": "true",
    # TLS terminates at the gateway in every environment.
    "MITOL_SECURE_SSL_REDIRECT": "False",
}

# Nothing routes to edx_content (learn-ai's task_routes is empty), but the
# deployed stacks run a worker for it.
CELERY_QUEUE_NAMES = ["default", "edx_content"]

# Above a fresh pod's 523-595MiB RSS (production, 7d to 2026-10-15). After a
# respawn the old worker can drain streamed responses beside it for ~22 minutes.
GRANIAN_WORKER_STARTUP_RSS = 600


class LearnAiHostnames(BaseModel):
    """Public hosts the application is served from."""

    model_config = ConfigDict(extra="forbid")

    api: str
    """The host the application is reached on directly."""
    frontend: str
    """The frontend's host; only ``cors_allow_origins`` reads it."""
    learn_api: str
    """MIT Learn's API host."""


class LearnAiSizing(BaseModel):
    """Requests and limits per workload. The defaults are the deployed stacks'."""

    model_config = ConfigDict(extra="forbid")

    # The limit leaves burst room above the 600-630Mi working set for a shell or
    # management command exec'd into a live pod, and sets the Granian RSS cap:
    # 0.9 * 2048 - GRANIAN_WORKER_STARTUP_RSS = 1243MiB.
    web: WorkloadResources = Field(
        default_factory=lambda: WorkloadResources(
            requests={"cpu": "100m", "memory": "1000Mi"}, limits={"memory": "2048Mi"}
        )
    )
    # Beat imports litellm at boot (main/apps.py) though it runs no LLM code; at
    # a 1000Mi limit it restarted ~25 times a week.
    celery_beat: WorkloadResources = Field(
        default_factory=lambda: WorkloadResources(
            requests={"cpu": "10m", "memory": "1000Mi"}, limits={"memory": "1536Mi"}
        )
    )
    # Each worker's two prefork children (the component's --concurrency=2) each
    # re-pay the litellm import; the working set runs 815-930Mi.
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
    oidc_secret_name: str | None = None
    application_docker_tag: str | None = None
    application_image_digest: str | None = None
    application_version: str | None = None
    """The release the image was built for; labels the pods."""
    cluster: ClusterCapabilities
    service_account_name: str | Output[str] | None = None
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
    """Return the environment, with the k8s labels in ``OTEL_RESOURCE_ATTRIBUTES``."""
    env_vars = {**LOCAL_AND_DEPLOYED_ENV, **bindings.environment_variables}
    merge_otel_resource_attributes(env_vars, bindings.k8s_labels)
    return env_vars


def application_config(bindings: LearnAiBindings) -> OLApplicationK8sConfig:
    """Return the ``OLApplicationK8s`` config for these bindings."""
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
            # Above the 14.7-19.5s a respawned worker took to start in production
            # (2026-10-14); see GranianConfig.respawn_interval.
            respawn_interval=60,
            worker_startup_rss=GRANIAN_WORKER_STARTUP_RSS,
            runtime_mode=None,
            # A holding value until the async-apps stage of
            # docs/plans/granian-configuration-overhaul.md sizes learn_ai.
            runtime_threads=2,
            no_ws=False,
            backlog=None,
            log_level="debug",
            application_module="main.asgi:application",
            enable_metrics=True,
            # Granian serves /static/*, which is why import_nginx_config is False.
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
        # Nothing here runs createcachetable: a fresh database needs learn-ai's
        # durable and external_assets DatabaseCache tables created out of band.
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
        webapp_keda_config=bindings.webapp_keda,
        manage_webapp_autoscaler=cluster.autoscalers,
        manage_celery_autoscalers=cluster.autoscalers,
        manage_webapp_memory_vpa=cluster.autoscalers,
        manage_pod_monitor=cluster.pod_monitors,
    )


def learn_session(bindings: LearnAiBindings) -> LearnSession:
    """Return the MIT Learn session the /ai/* routes share."""
    return LearnSession.for_host(bindings.env_suffix, bindings.hostnames.learn_api)


def prefixed_route_group(bindings: LearnAiBindings) -> OLApisixOIDCRouteGroup:
    """Return where the /ai/* routes on MIT Learn's host are served.

    The post-logout landing is mit-learn's /logout/: the session being destroyed
    is the shared one, and learn-ai has no logout view of its own.
    """
    return OLApisixOIDCRouteGroup(
        hosts=[bindings.hostnames.learn_api],
        path_prefix=API_PATH_PREFIX,
        login_paths=["/admin/login/*", "/http/login/*"],
        logout_entry_paths=["/logout", "/logout/"],
        post_logout_redirect_uri=learn_session(bindings).logout_landing_uri,
    )


def _oidc_credentials(
    bindings: LearnAiBindings,
) -> OLApisixOIDCVaultCredentials | OLApisixOIDCSecretCredentials:
    return oidc_credentials(
        vault_auth_name=bindings.cluster.vault_auth_name,
        secret_name=bindings.oidc_secret_name,
        vault_path=LearnSession.vault_path,
    )


def direct_oidc_config(bindings: LearnAiBindings) -> OLApisixOIDCConfig:
    """Return the OIDC settings for the routes on learn-ai's own host.

    It sets no cookie domain: this host's session is never shared with mit-learn.
    """
    return OLApisixOIDCConfig(
        application_name="learn-ai",
        oidc_access_token_expires_leeway=FORWARDED_ACCESS_TOKEN_EXPIRES_LEEWAY_SECONDS,
        k8s_labels=bindings.k8s_labels,
        k8s_namespace=NAMESPACE,
        # The component default adds organization:*; this host's session is not
        # mit-learn's, and learn-ai's APISIX_USERDATA_MAP reads no such claim.
        oidc_scope="openid profile email",
        oidc_introspection_endpoint_auth_method="client_secret_basic",
        oidc_logout_path="/logout",
        oidc_post_logout_redirect_uri="/",
        oidc_session_idling_timeout=0,
        oidc_session_rolling_timeout=0,
        # Not the shared MIT Learn name: where this host sits under the Learn
        # cookie domain (local-dev's ai.learn.<root>), mit-learn's cookie is also
        # sent here and the names would collide.
        oidc_session_cookie_name=apisix_oidc_session_cookie_name(
            "learn-ai",
            bindings.env_suffix,
        ),
        oidc_use_session_secret=True,
        credentials=_oidc_credentials(bindings),
    )


def prefixed_oidc_config(bindings: LearnAiBindings) -> OLApisixOIDCConfig:
    """Return the OIDC settings for the /ai/* routes on MIT Learn's host.

    A separate resource from ``direct_oidc_config`` because the cookie domain
    lives on the plugin config. Without MIT Learn's domain, the ``reqauth`` login
    writes a host-only cookie under the shared name that shadows mit-learn's, and
    OIDC callbacks can fail (#5238).
    """
    return learn_session_oidc_config(
        learn_session(bindings),
        prefixed_route_group(bindings),
        application_name="learn-ai-mit-learn",
        k8s_labels=bindings.k8s_labels,
        k8s_namespace=NAMESPACE,
        credentials=_oidc_credentials(bindings),
    )


def cors_allow_origins(bindings: LearnAiBindings) -> list[str]:
    """Return the origins allowed to read learn-ai responses with credentials.

    The shared plugins serve both hosts, so this covers every frontend that calls
    either. It is not derived from ``CSRF_ALLOWED_ORIGINS``, which omits the
    learn.mit.edu pair on CI.
    """
    hostnames = bindings.hostnames
    return [
        f"https://{hostnames.frontend}",
        f"https://{hostnames.api}",
        f"https://{hostnames.learn_api}",
        *bindings.cors_frontend_origins,
    ]


def shared_plugins(
    bindings: LearnAiBindings,  # noqa: ARG001
) -> list[OLApisixPluginConfig]:
    """Return the plugins both route groups share, beyond the component defaults."""
    return [
        # Evicts the host-only default "session" cookie both hosts wrote while one
        # OIDC resource served them (before #5219); mit-learn's cleanup never runs
        # on /ai/* responses. Delete once those cookies have aged out.
        stale_session_cookie_cleanup_plugin(),
    ]


def proxy_rewrite_plugin() -> OLApisixPluginConfig:
    """Return the plugin that strips the /ai prefix before the upstream."""
    return OLApisixPluginConfig(
        name="proxy-rewrite",
        enable=True,
        config={"regex_uri": [f"/{API_PATH_PREFIX}/(.*)", "/$1"]},
    )


def _canvas_route(
    hosts: list[str], path: str, shared_plugin_config_name: str
) -> OLApisixRouteConfig:
    """Return the route for the canvas syllabus agent, which needs a canvas_token."""
    return OLApisixRouteConfig(
        route_name="canvas_syllabus_agent",
        priority=20,
        shared_plugin_config_name=shared_plugin_config_name,
        plugins=[
            OLApisixPluginConfig(
                name="key-auth",
                config={
                    "header": "canvas_token",
                },
            ),
        ],
        hosts=hosts,
        paths=[path],
        backend_service_name=WEBAPP_SERVICE_NAME,
        backend_service_port=WEBAPP_SERVICE_PORT_NAME,
        backend_resolve_granularity="service",
    )


def direct_route_configs(
    bindings: LearnAiBindings,
    oidc: OLApisixOIDCResources,
    *,
    shared_plugin_config_name: str,
) -> list[OLApisixRouteConfig]:
    """Return the routes served from learn-ai's own host.

    :param oidc: The resources built from ``direct_oidc_config``.
    """
    hosts = [bindings.hostnames.api]
    return [
        _canvas_route(hosts, "/http/canvas_*", shared_plugin_config_name),
        OLApisixRouteConfig(
            route_name="passauth",
            priority=2,
            shared_plugin_config_name=shared_plugin_config_name,
            plugins=[
                OLApisixPluginConfig(**oidc.get_full_oidc_plugin_config("pass")),
            ],
            hosts=hosts,
            paths=["/*"],
            backend_service_name=WEBAPP_SERVICE_NAME,
            backend_service_port=WEBAPP_SERVICE_PORT_NAME,
            backend_resolve_granularity="service",
        ),
        # Strip trailing slash from logout redirect. This route's `redirect`
        # overrides the shared config's (http_to_https) rather than merging.
        OLApisixRouteConfig(
            route_name="logout-redirect",
            priority=10,
            shared_plugin_config_name=shared_plugin_config_name,
            plugins=[
                OLApisixPluginConfig(
                    name="redirect",
                    config={
                        # direct_oidc_config's oidc_logout_path, handled by
                        # passauth's openid-connect plugin
                        "uri": "/logout",
                    },
                ),
            ],
            hosts=hosts,
            paths=["/logout/*"],
            backend_service_name=WEBAPP_SERVICE_NAME,
            backend_service_port=WEBAPP_SERVICE_PORT_NAME,
            backend_resolve_granularity="service",
        ),
        OLApisixRouteConfig(
            route_name="reqauth",
            priority=10,
            shared_plugin_config_name=shared_plugin_config_name,
            plugins=[
                OLApisixPluginConfig(**oidc.get_full_oidc_plugin_config("auth")),
            ],
            hosts=hosts,
            paths=[
                "/admin/login/*",
                "/http/login/*",
            ],
            backend_service_name=WEBAPP_SERVICE_NAME,
            backend_service_port=WEBAPP_SERVICE_PORT_NAME,
            backend_resolve_granularity="service",
        ),
        OLApisixRouteConfig(
            route_name="websocket",
            priority=1,
            websocket=True,
            shared_plugin_config_name=shared_plugin_config_name,
            plugins=[
                OLApisixPluginConfig(**oidc.get_full_oidc_plugin_config("pass")),
            ],
            hosts=hosts,
            paths=[
                "/ws/*",
            ],
            backend_service_name=WEBAPP_SERVICE_NAME,
            backend_service_port=WEBAPP_SERVICE_PORT_NAME,
            backend_resolve_granularity="service",
        ),
        # 204 is the EFF Do Not Track convention for "no policy published".
        # Without this route passauth sends every crawl to a Django 404.
        OLApisixRouteConfig(
            route_name="dnt-policy",
            priority=10,
            shared_plugin_config_name=shared_plugin_config_name,
            hosts=hosts,
            paths=["/.well-known/dnt-policy.txt"],
            backend_service_name=WEBAPP_SERVICE_NAME,
            backend_service_port=WEBAPP_SERVICE_PORT_NAME,
            backend_resolve_granularity="service",
            plugins=[
                OLApisixPluginConfig(
                    name="mocking",
                    secretRef=None,
                    config={
                        "response_status": 204,
                        "response_example": "",
                        "content_type": "text/plain",
                        "with_mock_header": False,
                    },
                ),
            ],
        ),
    ]


def prefixed_route_configs(
    bindings: LearnAiBindings,
    oidc: OLApisixOIDCResources,
    *,
    shared_plugin_config_name: str,
) -> list[OLApisixRouteConfig]:
    """Return the /ai/* routes served from MIT Learn's API host.

    :param oidc: The resources built from ``prefixed_oidc_config``.
    """
    route_group = prefixed_route_group(bindings)
    return [
        _canvas_route(
            route_group.hosts,
            f"/{API_PATH_PREFIX}/http/canvas_*",
            shared_plugin_config_name,
        ),
        # passauth, logout-redirect and reqauth.
        *route_group.route_configs(
            oidc,
            passauth_priority=2,
            shared_plugin_config_name=shared_plugin_config_name,
            rewrite_plugins=[proxy_rewrite_plugin()],
            backend_service_name=WEBAPP_SERVICE_NAME,
            backend_service_port=WEBAPP_SERVICE_PORT_NAME,
        ),
        OLApisixRouteConfig(
            route_name="websocket",
            priority=1,
            websocket=True,
            shared_plugin_config_name=shared_plugin_config_name,
            plugins=[
                proxy_rewrite_plugin(),
                OLApisixPluginConfig(**oidc.get_full_oidc_plugin_config("pass")),
            ],
            hosts=route_group.hosts,
            paths=[f"/{API_PATH_PREFIX}/ws/*"],
            backend_service_name=WEBAPP_SERVICE_NAME,
            backend_service_port=WEBAPP_SERVICE_PORT_NAME,
            backend_resolve_granularity="service",
        ),
    ]


def canvas_agent_consumer_spec(key: Input[str]) -> dict[str, Any]:
    """Return the ``spec`` of the ApisixConsumer the canvas agent authenticates as.

    :param key: The key the agent sends in the ``canvas_token`` header.
    """
    return {
        "ingressClassName": "apache-apisix",
        "authParameter": {"keyAuth": {"value": {"key": key}}},
    }
