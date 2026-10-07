# ruff: noqa: E501
"""What MITx Online is in every environment it runs in.

The environment variables, the ``OLApplicationK8sConfig`` and the APISIX routes
that do not depend on where the application runs live here. Everything that
does (hostnames, the Secrets to mount, the image, sizing, what the cluster can
do) arrives as a ``MitxonlineBindings``. The deployed program builds its
bindings from the AWS and Vault resources it creates. A local program builds
them from the local platform. See docs/plans/local-dev-deployed-parity.md.

Nothing in this module may read Pulumi config, a stack reference, or a cloud
API, so that importing it needs no provider.
"""

from pathlib import Path
from typing import Any, Literal

from kubernetes.utils.quantity import parse_quantity
from pulumi import Output
from pydantic import BaseModel, ConfigDict

from bridge.lib.constants import (
    apisix_oidc_session_cookie_name,
    mit_learn_session_cookie_name,
)
from bridge.lib.magic_numbers import STATIC_ASSET_MAX_AGE_SECONDS
from ol_infrastructure.components.services.apisix import (
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
    OLApisixPluginConfig,
    OLApisixRouteConfig,
    oidc_gateway_pre_function_plugin,
    stale_session_cookie_cleanup_plugin,
)
from ol_infrastructure.components.services.k8s import (
    GranianConfig,
    OLApplicationK8sCeleryBeatConfig,
    OLApplicationK8sCeleryRedisConfig,
    OLApplicationK8sCeleryWorkerConfig,
    OLApplicationK8sConfig,
    OLApplicationK8sDevShellConfig,
    OLApplicationK8sKedaWebappScalingConfig,
)
from ol_infrastructure.lib.ol_types import Services
from ol_infrastructure.lib.pulumi_helper import merge_otel_resource_attributes

NAMESPACE = "mitxonline"
WEBAPP_SERVICE_NAME = "mitxonline-webapp"
WEBAPP_SERVICE_PORT_NAME = "http"
# The MIT Learn API host serves this application under /mitxonline/*.
API_PATH_PREFIX = "mitxonline"
WEB_MEMORY_CEILING = "3Gi"

# Directories Tilt syncs into the container that hold no Python.
RELOAD_IGNORE_DIRS = ["frontend", "static", "staticfiles"]

ENVIRONMENT_VARIABLES = {
    "CRON_COURSERUN_SYNC_HOURS": "*",
    "FEATURE_IGNORE_EDX_FAILURES": "True",
    "FEATURE_SYNC_ON_DASHBOARD_LOAD": "False",
    "HUBSPOT_PIPELINE_ID": "19817792",
    "MITOL_GOOGLE_SHEETS_REFUNDS_COMPLETED_DATE_COL": "12",
    "MITOL_GOOGLE_SHEETS_REFUNDS_ERROR_COL": "13",
    "MITOL_GOOGLE_SHEETS_REFUNDS_SKIP_ROW_COL": "14",
    "MITX_ONLINE_ADMIN_EMAIL": "cuddle-bunnies@mit.edu",
    "MITX_ONLINE_DB_CONN_MAX_AGE": "0",
    "MITX_ONLINE_DB_DISABLE_SSL": "True",  # pgbouncer buildpack uses stunnel to handle encryption
    "MITX_ONLINE_FROM_EMAIL": "MIT Learn <mitlearn-support@mit.edu>",
    "MITX_ONLINE_OAUTH_PROVIDER": "mitxonline-oauth2",
    "MITX_ONLINE_REPLY_TO_ADDRESS": "MIT Learn <mitlearn-support@mit.edu>",
    "MITX_ONLINE_SECURE_SSL_REDIRECT": "False",
    "MITX_ONLINE_SUPPORT_EMAIL": "mitlearn-support@mit.edu",
    "MITX_ONLINE_USE_S3": "True",
    "NODE_MODULES_CACHE": "False",
    "OPENEDX_SERVICE_WORKER_USERNAME": "login_service_user",
    "OPEN_EXCHANGE_RATES_URL": "https://openexchangerates.org/api/",
    "POSTHOG_API_HOST": "https://ph.ol.mit.edu",
    "POSTHOG_ENABLED": "True",
    "SITE_NAME": "MITx Online",
    "USE_X_FORWARDED_HOST": "True",
    "ZENDESK_HELP_WIDGET_ENABLED": "True",
}


class CeleryQueue(BaseModel):
    """A queue the application routes tasks to, and what its worker is given."""

    name: str
    resource_requests: dict[str, str]
    resource_limits: dict[str, str]


CELERY_QUEUES = [
    CeleryQueue(
        name="celery",
        resource_requests={"cpu": "100m", "memory": "2Gi"},
        resource_limits={"memory": "2Gi"},
    ),
    CeleryQueue(
        name="hubspot_sync",
        resource_requests={"cpu": "100m", "memory": "1Gi"},
        resource_limits={"memory": "1Gi"},
    ),
]


class MitxonlineHostnames(BaseModel):
    """Public hosts the application is served from."""

    api: str
    frontend: str
    learn_api: str


class ClusterCapabilities(BaseModel):
    """What the target cluster provides beyond plain Kubernetes.

    The defaults describe EKS. A cluster without the AWS VPC CNI, ECR, KEDA, the
    VPA or the Prometheus operator turns the matching capability off.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    registry: Literal["dockerhub", "ecr", "direct"] = "ecr"
    security_group_id: Output[str] | None = None
    security_group_name: Output[str] | None = None
    vault_auth_name: str | None = None
    autoscalers: bool = True
    pod_monitors: bool = True


class MitxonlineBindings(BaseModel):
    """Everything about MITx Online that differs between environments."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    env_suffix: str
    hostnames: MitxonlineHostnames
    environment_variables: dict[str, Any]
    """Values for this environment, applied over ``ENVIRONMENT_VARIABLES``. Must
    carry ``OPENEDX_API_BASE_URL``, which the routes read for a response header."""
    k8s_labels: dict[str, str]
    secret_names: list[str]
    application_docker_tag: str | None = None
    application_image_digest: str | None = None
    cluster: ClusterCapabilities
    min_replicas: int
    web_memory_limit: str
    granian_worker_startup_rss: int | None = None
    granian_reload: bool = False
    dev_shell: bool = False
    slack_channel: str | None = None
    celery_redis: OLApplicationK8sCeleryRedisConfig | None = None
    celery_topology: Literal["per-queue", "merged"] = "per-queue"
    """``merged`` runs one worker Deployment consuming every queue, for clusters
    where a Deployment per queue costs more than it tells a developer."""
    webapp_keda: OLApplicationK8sKedaWebappScalingConfig | None = None


def environment_variables(bindings: MitxonlineBindings) -> dict[str, Any]:
    """Return the application's environment for these bindings.

    :param bindings: The environment being rendered for.
    :returns: Variable names mapped to values, with the k8s labels folded into
        ``OTEL_RESOURCE_ATTRIBUTES``.
    :rtype: dict[str, Any]
    """
    env_vars = {**ENVIRONMENT_VARIABLES, **bindings.environment_variables}
    # Unconditionally append k8s labels to OTEL_RESOURCE_ATTRIBUTES so all telemetry
    # carries organizational metadata regardless of stack environment.
    merge_otel_resource_attributes(env_vars, bindings.k8s_labels)
    return env_vars


def celery_worker_configs(
    topology: Literal["per-queue", "merged"],
) -> list[OLApplicationK8sCeleryWorkerConfig]:
    """Return the celery workers for a topology.

    :param topology: ``per-queue`` for one Deployment per queue, ``merged`` for
        one Deployment consuming all of them.
    :returns: Worker configs for ``OLApplicationK8sConfig.celery_worker_configs``.
    :rtype: list[OLApplicationK8sCeleryWorkerConfig]
    """
    if topology == "merged":
        largest = max(
            CELERY_QUEUES,
            key=lambda queue: parse_quantity(queue.resource_limits["memory"]),
        )
        return [
            OLApplicationK8sCeleryWorkerConfig(
                # Explicit because the name lands in the Deployment name and a
                # label value, where the comma in queue_name is not valid.
                worker_name="all",
                queue_name=",".join(queue.name for queue in CELERY_QUEUES),
                resource_requests=largest.resource_requests,
                resource_limits=largest.resource_limits,
            )
        ]
    return [
        OLApplicationK8sCeleryWorkerConfig(
            queue_name=queue.name,
            resource_requests=queue.resource_requests,
            resource_limits=queue.resource_limits,
        )
        for queue in CELERY_QUEUES
    ]


def application_config(bindings: MitxonlineBindings) -> OLApplicationK8sConfig:
    """Return the ``OLApplicationK8s`` config for these bindings.

    :param bindings: The environment being rendered for.
    :returns: The config to hand to ``OLApplicationK8s``.
    :rtype: OLApplicationK8sConfig
    """
    cluster = bindings.cluster
    return OLApplicationK8sConfig(
        project_root=Path(__file__).parent,
        application_config=environment_variables(bindings),
        application_name=Services.mitxonline,
        application_namespace=NAMESPACE,
        application_lb_service_name=WEBAPP_SERVICE_NAME,
        application_lb_service_port_name=WEBAPP_SERVICE_PORT_NAME,
        application_min_replicas=bindings.min_replicas,
        k8s_global_labels=bindings.k8s_labels,
        env_from_secret_names=bindings.secret_names,
        application_security_group_id=cluster.security_group_id,
        application_security_group_name=cluster.security_group_name,
        application_image_repository="mitodl/mitxonline-app",
        application_docker_tag=bindings.application_docker_tag,
        application_image_digest=bindings.application_image_digest,
        application_cmd_array=["uwsgi"],
        application_arg_array=["/tmp/uwsgi.ini"],  # noqa: S108
        granian_config=GranianConfig(
            # One worker, scaled with replicas, per the component defaults. Blocking
            # threads are pinned above the component's 8: over the 14 days to
            # 2026-09-17 the busiest pod peaked at 11.7 concurrently-busy threads
            # (during the 2026-09-16 edxapp Deployment replacement, with requests
            # stalled on edX) and 6.8 outside it (a 35-minute edX slowdown on
            # 2026-09-14, CPU under 0.13 cores per pod). p99 of the busiest pod was
            # 0.78. 16 covers both stalls. Backpressure takes the component default;
            # peak connections were 36 per pod.
            # See docs/plans/granian-configuration-overhaul.md
            blocking_threads=16,
            blocking_threads_idle_timeout=120,
            worker_startup_rss=bindings.granian_worker_startup_rss,
            enable_metrics=True,
            # Serve /static/* from Granian's Rust layer instead of the sidecar
            # (docs/plans/remove-nginx-sidecar.md, stage 5), same shape as
            # ocw_studio/xpro. STATIC_ROOT is /src/staticfiles, the same
            # emptyDir the collectstatic init container populates, and
            # STATIC_URL is Granian's default /static route.
            static_path_mounts=["/src/staticfiles"],
            static_path_expires=STATIC_ASSET_MAX_AGE_SECONDS,
            reload=bindings.granian_reload,
            reload_ignore_dirs=RELOAD_IGNORE_DIRS if bindings.granian_reload else [],
            # Every reload waits this long for a worker whose application threads
            # keep it from stopping, so it stays short.
            workers_kill_timeout=1 if bindings.granian_reload else None,
        ),
        slack_channel=bindings.slack_channel,
        vault_k8s_resource_auth_name=cluster.vault_auth_name,
        registry=cluster.registry,
        import_nginx_config=False,
        import_uwsgi_config=True,
        init_migrations=False,
        init_collectstatic=True,
        pre_deploy_commands=[
            ("migrate", ["python", "manage.py", "migrate", "--noinput"])
        ],
        # Sized on its own rather than inheriting the webapp's limit: two production
        # migrate runs on 2026-09-28 peaked at 2754MiB and 2766MiB working set, which
        # the webapp's 2000Mi would OOMKill, and the Deployment waits on this Job.
        pre_deploy_resource_requests={"cpu": "250m", "memory": "3Gi"},
        pre_deploy_resource_limits={"memory": "3Gi"},
        celery_redis_config=bindings.celery_redis,
        celery_worker_configs=celery_worker_configs(bindings.celery_topology),
        celery_beat_config=OLApplicationK8sCeleryBeatConfig(
            resource_requests={"cpu": "10m", "memory": "384Mi"},
            resource_limits={"memory": "384Mi"},
        ),
        # An unrouted, launch-on-request pod in the app image for developers to
        # run manage.py commands without being OOMKilled or scaled away. Created
        # at 0 replicas; scale it up to use it and back down when done.
        #   kubectl -n mitxonline scale deploy/mitxonline-dev-shell --replicas=1
        #   kubectl -n mitxonline exec -it deploy/mitxonline-dev-shell -- bash
        #   kubectl -n mitxonline scale deploy/mitxonline-dev-shell --replicas=0
        dev_shell_config=OLApplicationK8sDevShellConfig()
        if bindings.dev_shell
        else None,
        resource_requests={"cpu": "250m", "memory": bindings.web_memory_limit},
        resource_limits={"memory": bindings.web_memory_limit},
        # Memory is managed vertically by the component's webapp VPA, between the
        # declared limit above and the ceiling below. Horizontal scaling is KEDA-driven,
        # so hpa_scaling_metrics is unused -- the component builds a ScaledObject
        # instead of a native HPA when webapp_keda_config is set.
        webapp_vpa_max_allowed_memory=WEB_MEMORY_CEILING,
        webapp_keda_config=bindings.webapp_keda,
        manage_webapp_autoscaler=cluster.autoscalers,
        manage_celery_autoscalers=cluster.autoscalers,
        manage_webapp_memory_vpa=cluster.autoscalers,
        manage_pod_monitor=cluster.pod_monitors,
    )


def _oidc_config(bindings: MitxonlineBindings, **settings: Any) -> OLApisixOIDCConfig:
    vault_auth_name = bindings.cluster.vault_auth_name
    if vault_auth_name is None:
        msg = (
            "OLApisixOIDCResources reads the OIDC client credentials through Vault, "
            "so the routes need cluster.vault_auth_name."
        )
        raise ValueError(msg)
    return OLApisixOIDCConfig(
        k8s_labels=bindings.k8s_labels,
        k8s_namespace=NAMESPACE,
        oidc_session_absolute_timeout=60 * 20160,
        oidc_session_idling_timeout=0,
        oidc_session_rolling_timeout=0,
        oidc_use_session_secret=True,
        vault_mount="secret-operations",
        vault_mount_type="kv-v1",
        vault_path="sso/mitlearn",
        vaultauth=vault_auth_name,
        **settings,
    )


def direct_oidc_config(bindings: MitxonlineBindings) -> OLApisixOIDCConfig:
    """Return the OIDC settings for the routes on MITx Online's own hosts.

    :param bindings: The environment being rendered for.
    :returns: The config to hand to ``OLApisixOIDCResources``.
    :rtype: OLApisixOIDCConfig
    """
    api_domain = bindings.hostnames.api
    return _oidc_config(
        bindings,
        application_name="mitxonline-k8s-no-prefix",
        oidc_logout_path="/logout/oidc",
        oidc_post_logout_redirect_uri=f"https://{api_domain}/logout/",
        oidc_session_cookie_domain=api_domain.removeprefix("api"),
        # This is MITx Online's own login session, on its own parent domain --
        # distinct from the MIT Learn session the prefixed resources below read,
        # so it gets its own name rather than the shared one.  It needs an
        # explicit name for the same reason mit-learn does: the cookie domain
        # above means the Production cookie is also sent to
        # rc./ci.mitxonline.mit.edu, where a same-named cookie belonging to
        # another environment cannot be decrypted.
        oidc_session_cookie_name=apisix_oidc_session_cookie_name(
            "mitxonline",
            bindings.env_suffix,
        ),
    )


def prefixed_oidc_config(bindings: MitxonlineBindings) -> OLApisixOIDCConfig:
    """Return the OIDC settings for the /mitxonline/* routes on MIT Learn's host.

    :param bindings: The environment being rendered for.
    :returns: The config to hand to ``OLApisixOIDCResources``.
    :rtype: OLApisixOIDCConfig
    """
    learn_api_domain = bindings.hostnames.learn_api
    return _oidc_config(
        bindings,
        application_name="mitxonline-k8s",
        oidc_logout_path=f"/{API_PATH_PREFIX}/logout/oidc",
        # The MIT Learn host, not MITx Online's own: the prefixed path only
        # exists on the host these routes are served from, so naming api_domain
        # here pointed the tail of the logout at
        # api.<env>.mitxonline.mit.edu/mitxonline/logout/, which the catch-all
        # "passauth" route proxies through unrewritten and Django answers with a
        # 404 (verified on CI) -- so every RP-initiated logout on this group
        # ended on an error page.
        oidc_post_logout_redirect_uri=f"https://{learn_api_domain}/{API_PATH_PREFIX}/logout/",
        # These routes are served from MIT Learn's own host
        # (api.<env>.learn.mit.edu) and are what the MIT Learn frontend calls as
        # NEXT_PUBLIC_MITX_ONLINE_BASE_URL.  Their unauth_action="pass" plugins
        # recognize the session mit-learn's login flow set, which only works
        # while both name the cookie identically -- so this must track
        # mit_learn/__main__.py's oidc_session_cookie_name, not the mitxonline
        # name used above.
        oidc_session_cookie_name=mit_learn_session_cookie_name(bindings.env_suffix),
        # Matching mit_learn/__main__.py's cookie domain matters as much as
        # matching its name.  The "reqauth" route below performs a real login
        # (unauth_action="auth" on /mitxonline/login/), and without a domain the
        # plugin would write a *host-only* cookie on api.<env>.learn.mit.edu
        # under the shared name -- a second, separate entry in the browser's jar
        # shadowing mit-learn's .learn.mit.edu cookie on that host.  With the
        # domain set, all of them read and write the one shared cookie.
        oidc_session_cookie_domain=learn_api_domain.removeprefix("api"),
    )


def shared_plugins(bindings: MitxonlineBindings) -> list[OLApisixPluginConfig]:
    """Return the plugins both route groups share, beyond the component defaults.

    :param bindings: The environment being rendered for.
    :returns: Plugins for ``OLApisixSharedPluginsConfig.plugins``.
    :rtype: list[OLApisixPluginConfig]
    """
    return [
        # 285 callbacks a day on mitxonline.mit.edu come back from Keycloak
        # with error=temporarily_unavailable instead of a code, and the
        # openid-connect plugin serves each one a 500.  Unlike the cookie
        # cleanup below, this is safe to attach here rather than per route
        # group: it derives its redirect target from the request URI and
        # its guard cookie is host-only, so neither depends on which parent
        # domain a group's session cookie was scoped to.  Ditto the
        # canonical-origin redirect it also carries, which is derived from
        # the request's own host.
        #
        # Both cookie names, because this config is also referenced by the
        # /mitxonline/* routes on MIT Learn's host, which log in under MIT
        # Learn's session.  Omitting either would divert every successful
        # login on the routes that use it.
        oidc_gateway_pre_function_plugin(
            session_cookie_names=[
                apisix_oidc_session_cookie_name("mitxonline", bindings.env_suffix),
                mit_learn_session_cookie_name(bindings.env_suffix),
            ],
        ),
    ]


def _frame_ancestors_plugin(bindings: MitxonlineBindings) -> OLApisixPluginConfig:
    openedx_base_url = environment_variables(bindings)["OPENEDX_API_BASE_URL"]
    return OLApisixPluginConfig(
        name="response-rewrite",
        config={
            "headers": {
                "set": {
                    "Content-Security-Policy": f"frame-ancestors 'self' {openedx_base_url}"
                }
            }
        },
    )


def _uncached_hash_plugin() -> OLApisixPluginConfig:
    return OLApisixPluginConfig(
        name="response-rewrite",
        secretRef=None,
        config={"headers": {"set": {"Cache-Control": "private, no-cache"}}},
    )


def direct_route_configs(
    bindings: MitxonlineBindings,
    oidc: OLApisixOIDCResources,
    shared_plugin_config_name: str,
) -> list[OLApisixRouteConfig]:
    """Return the routes served from MITx Online's own hosts.

    :param bindings: The environment being rendered for.
    :param oidc: The OIDC resources built from ``direct_oidc_config``.
    :param shared_plugin_config_name: Name of the ApisixPluginConfig carrying
        ``shared_plugins``.
    :returns: Route configs for one ``OLApisixRoute``.
    :rtype: list[OLApisixRouteConfig]
    """
    hosts = [bindings.hostnames.api, bindings.hostnames.frontend]
    frame_ancestors = _frame_ancestors_plugin(bindings)
    # Both OIDC resources moved off lua-resty-session's default "session" cookie
    # name, so every current user has a dead one in their browser.  The two route
    # groups need different deletions because a cookie's identity includes its
    # domain: the direct routes' cookie was scoped to the mitxonline parent domain,
    # while the prefixed routes' was host-only on MIT Learn's API host.  These are
    # attached per route group rather than to the shared plugin config because that
    # config is referenced from both groups, and a Domain=.mitxonline.mit.edu
    # deletion emitted from api.<env>.learn.mit.edu is simply rejected by the
    # browser.  Safe to delete once the old cookies have aged out of circulation.
    stale_session_cleanup = stale_session_cookie_cleanup_plugin(
        cookie_domains=[bindings.hostnames.api.removeprefix("api")],
    )
    return [
        OLApisixRouteConfig(
            route_name="passauth",
            priority=0,
            hosts=hosts,
            paths=["/*"],
            shared_plugin_config_name=shared_plugin_config_name,
            plugins=[
                oidc.get_full_oidc_plugin_config(unauth_action="pass"),
                frame_ancestors,
                stale_session_cleanup,
            ],
            backend_service_name=WEBAPP_SERVICE_NAME,
            backend_service_port=WEBAPP_SERVICE_PORT_NAME,
        ),
        OLApisixRouteConfig(
            route_name="logout-redirect",
            priority=10,
            hosts=hosts,
            paths=["/logout/oidc/*"],
            plugins=[
                OLApisixPluginConfig(name="redirect", config={"uri": "/logout/oidc"}),
                frame_ancestors,
                stale_session_cleanup,
            ],
            shared_plugin_config_name=shared_plugin_config_name,
            backend_service_name=WEBAPP_SERVICE_NAME,
            backend_service_port=WEBAPP_SERVICE_PORT_NAME,
        ),
        OLApisixRouteConfig(
            route_name="reqauth",
            priority=10,
            hosts=hosts,
            paths=["/login/*", "/admin/login/*", "/login*", "/login/oidc*"],
            plugins=[
                oidc.get_full_oidc_plugin_config(unauth_action="auth"),
                frame_ancestors,
                stale_session_cleanup,
            ],
            shared_plugin_config_name=shared_plugin_config_name,
            backend_service_name=WEBAPP_SERVICE_NAME,
            backend_service_port=WEBAPP_SERVICE_PORT_NAME,
        ),
        OLApisixRouteConfig(
            route_name="cart",
            priority=20,
            hosts=hosts,
            paths=[
                "/cart/",
                "/cart",
                "/cart/*",
            ],
            plugins=[
                oidc.get_full_oidc_plugin_config(unauth_action="auth"),
                frame_ancestors,
                stale_session_cleanup,
            ],
            shared_plugin_config_name=shared_plugin_config_name,
            backend_service_name=WEBAPP_SERVICE_NAME,
            backend_service_port=WEBAPP_SERVICE_PORT_NAME,
        ),
        # The `location` blocks that used to live in the nginx sidecar
        # (docs/plans/remove-nginx-sidecar.md, stage 5). nginx resolved
        # hash.txt against `root /src` with a `try_files` fallback to
        # /staticfiles, i.e. /src/static/hash.txt first; Granian's
        # static_path_mounts instead serves /src/staticfiles/hash.txt --
        # same content only if something copies it there.
        OLApisixRouteConfig(
            route_name="static-hash",
            priority=20,
            hosts=hosts,
            paths=["/static/hash.txt"],
            shared_plugin_config_name=shared_plugin_config_name,
            backend_service_name=WEBAPP_SERVICE_NAME,
            backend_service_port=WEBAPP_SERVICE_PORT_NAME,
            plugins=[_uncached_hash_plugin()],
        ),
        # The sidecar answered this with a 204 (EFF Do Not Track convention
        # for "no policy published"). "passauth" above would otherwise proxy
        # it through to Django, which has no view for it -- kept as a mock so
        # a crawled path doesn't burn a Granian blocking thread on a 404.
        OLApisixRouteConfig(
            route_name="dnt-policy",
            priority=10,
            # Referenced no plugin config, unlike every sibling here, so
            # this path emitted no prometheus series and no OTLP span. `mocking`
            # short-circuits before the upstream but `prometheus` runs in the log
            # phase, so the shared config still records it -- and cors/gzip are
            # no-ops on an empty 204.
            shared_plugin_config_name=shared_plugin_config_name,
            hosts=hosts,
            paths=["/.well-known/dnt-policy.txt"],
            backend_service_name=WEBAPP_SERVICE_NAME,
            backend_service_port=WEBAPP_SERVICE_PORT_NAME,
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
    bindings: MitxonlineBindings,
    oidc: OLApisixOIDCResources,
    shared_plugin_config_name: str,
) -> list[OLApisixRouteConfig]:
    """Return the /mitxonline/* routes served from MIT Learn's API host.

    :param bindings: The environment being rendered for.
    :param oidc: The OIDC resources built from ``prefixed_oidc_config``.
    :param shared_plugin_config_name: Name of the ApisixPluginConfig carrying
        ``shared_plugins``.
    :returns: Route configs for one ``OLApisixRoute``.
    :rtype: list[OLApisixRouteConfig]
    """
    hosts = [bindings.hostnames.learn_api]
    frame_ancestors = _frame_ancestors_plugin(bindings)
    stale_session_cleanup = stale_session_cookie_cleanup_plugin()
    strip_prefix = OLApisixPluginConfig(
        name="proxy-rewrite",
        config={
            "regex_uri": [
                f"/{API_PATH_PREFIX}/(.*)",
                "/$1",
            ],
        },
    )
    return [
        OLApisixRouteConfig(
            route_name="passauth",
            priority=0,
            hosts=hosts,
            paths=[f"/{API_PATH_PREFIX}/*"],
            shared_plugin_config_name=shared_plugin_config_name,
            plugins=[
                strip_prefix,
                oidc.get_full_oidc_plugin_config(unauth_action="pass"),
                frame_ancestors,
                stale_session_cleanup,
            ],
            backend_service_name=WEBAPP_SERVICE_NAME,
            backend_service_port=WEBAPP_SERVICE_PORT_NAME,
        ),
        # Strips the trailing slash onto this group's own logout path.  It named
        # the unprefixed "/logout/oidc", which on this host is mit-learn's
        # plugin: the shared cookie meant the session did get destroyed, but the
        # post-logout redirect then came from mit-learn's resource, so a user
        # logging out of MITx Online landed on a mit-learn page and never
        # reached the Open edX logout fan-out their own logout view performs.
        # Their MITx Online session cookie also outlived the logout, though
        # ApisixUserMiddleware inherits force_logout_if_no_header from
        # RemoteUserMiddleware and drops it on the next request.
        OLApisixRouteConfig(
            route_name="logout-redirect",
            priority=10,
            hosts=hosts,
            paths=[f"/{API_PATH_PREFIX}/logout/oidc/*"],
            plugins=[
                OLApisixPluginConfig(
                    name="redirect",
                    config={"uri": f"/{API_PATH_PREFIX}/logout/oidc"},
                ),
                frame_ancestors,
                stale_session_cleanup,
            ],
            shared_plugin_config_name=shared_plugin_config_name,
            backend_service_name=WEBAPP_SERVICE_NAME,
            backend_service_port=WEBAPP_SERVICE_PORT_NAME,
        ),
        OLApisixRouteConfig(
            route_name="reqauth",
            priority=10,
            hosts=hosts,
            paths=[
                f"/{API_PATH_PREFIX}/login/",
                f"/{API_PATH_PREFIX}/login/oidc*",
                f"/{API_PATH_PREFIX}/admin/login/*",
                f"/{API_PATH_PREFIX}/login",
            ],
            plugins=[
                strip_prefix,
                oidc.get_full_oidc_plugin_config(unauth_action="auth"),
                frame_ancestors,
                stale_session_cleanup,
            ],
            shared_plugin_config_name=shared_plugin_config_name,
            backend_service_name=WEBAPP_SERVICE_NAME,
            backend_service_port=WEBAPP_SERVICE_PORT_NAME,
        ),
        # Static assets requested through this prefix (frontend references
        # like /mitxonline/static/...) rewrite through "passauth" the same way,
        # so hash.txt needs the same override here. No dnt-policy counterpart:
        # /.well-known/dnt-policy.txt is a root-relative convention no client
        # ever requests under a path prefix, so unlike hash.txt it was never
        # reachable through this resource even with the sidecar -- same
        # reasoning as mit_learn's /learn/*-prefixed resource. See
        # docs/plans/remove-nginx-sidecar.md.
        OLApisixRouteConfig(
            route_name="static-hash",
            priority=20,
            hosts=hosts,
            paths=[f"/{API_PATH_PREFIX}/static/hash.txt"],
            shared_plugin_config_name=shared_plugin_config_name,
            plugins=[strip_prefix, _uncached_hash_plugin()],
            backend_service_name=WEBAPP_SERVICE_NAME,
            backend_service_port=WEBAPP_SERVICE_PORT_NAME,
        ),
    ]
