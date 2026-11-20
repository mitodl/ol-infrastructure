"""What ol-analytics-api is in every environment it runs in.

Everything that differs between environments arrives as an
``OlAnalyticsApiBindings``; see docs/plans/local-dev-deployed-parity.md.

Nothing in this module may read Pulumi config, a stack reference, a cloud API,
or ``os.environ``, so that importing it needs no provider.
"""

from pathlib import Path
from typing import Any

import pulumi_kubernetes as kubernetes
from pulumi import Output
from pydantic import BaseModel, ConfigDict, Field

from bridge.lib.constants import LearnSession
from ol_infrastructure.applications.ol_analytics_api.vault_secrets import (
    vault_secrets,
)
from ol_infrastructure.components.services.apisix import (
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
    OLApisixOIDCRouteGroup,
    OLApisixOIDCVaultCredentials,
    OLApisixPluginConfig,
    OLApisixRouteConfig,
    learn_session_oidc_config,
)
from ol_infrastructure.components.services.k8s import (
    GranianConfig,
    OLApplicationK8sConfig,
)
from ol_infrastructure.lib.app_bindings import (
    ClusterCapabilities,
    WorkloadResources,
    oidc_credentials,
)
from ol_infrastructure.lib.pulumi_helper import merge_otel_resource_attributes
from ol_infrastructure.lib.secret_env import secret_named

__all__ = [
    "APPLICATION_NAME",
    "APPLICATION_PORT",
    "NAMESPACE",
    "SERVICE_NAME",
    "SERVICE_PORT_NAME",
    "STARROCKS_VAULT_MOUNT",
    "ClusterCapabilities",
    "OlAnalyticsApiBindings",
    "OlAnalyticsApiHostnames",
    "StarRocksVaultLogin",
    "WorkloadResources",
    "application_config",
    "canonical_oidc_config",
    "canonical_route_configs",
    "cors_allow_origins",
    "environment_variables",
    "hosts",
    "learn_oidc_config",
    "learn_route_config",
    "learn_session",
]

APPLICATION_NAME = "ol-analytics-api"
# The deployed namespace, declared by the data EKS stack (`eks:namespaces`).
NAMESPACE = "ol-analytics"
SERVICE_NAME = APPLICATION_NAME
SERVICE_PORT_NAME = "http"
# Granian listens here. No nginx sidecar sits in front, so the Service and the
# health probes target it directly.
APPLICATION_PORT = 8000
# The Vault database engine the pod reads its StarRocks credentials from. Not
# environment-specific: each environment has its own Vault server.
STARROCKS_VAULT_MOUNT = "database-starrocks"

_BACKEND: dict[str, Any] = {
    "backend_service_name": SERVICE_NAME,
    "backend_service_port": SERVICE_PORT_NAME,
    "backend_resolve_granularity": "service",
}


class OlAnalyticsApiHostnames(BaseModel):
    """Public hosts the application is served from."""

    model_config = ConfigDict(extra="forbid")

    canonical: str | None
    """The service's own host, behind its dedicated Keycloak client. None where
    only the Learn-scoped host is served."""
    learn: str | None
    """The host under MIT Learn's parent domain the MIT Learn frontend calls,
    sharing mit-learn's session."""


class StarRocksVaultLogin(BaseModel):
    """Where the pod logs in to Vault for its own StarRocks credentials.

    The app does a direct hvac Kubernetes login with its ServiceAccount token
    (core/db/vault_credentials.py); without these variables it reads
    STARROCKS_USER and STARROCKS_PASSWORD from its environment instead.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    address: str
    k8s_auth_mount: str | Output[str]
    """The cluster's Kubernetes auth mount on that Vault server."""


class OlAnalyticsApiBindings(BaseModel):
    """Everything about ol-analytics-api that differs between environments."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    env_suffix: str
    namespace: str = NAMESPACE
    hostnames: OlAnalyticsApiHostnames
    environment_variables: dict[str, Any]
    starrocks_vault: StarRocksVaultLogin | None = None
    cors_frontend_origins: list[str]
    """Origins beyond the canonical host that call the API with credentials."""
    k8s_labels: dict[str, str]
    secret_names: list[str]
    oidc_secret_name: str | None = None
    """A Secret in the namespace holding the Learn-scoped routes' client
    credentials, where Vault does not supply them."""
    application_docker_tag: str | None = None
    application_image_digest: str | None = None
    application_version: str | None = None
    """The release the image was built for; labels the pods."""
    cluster: ClusterCapabilities
    service_account_name: str | Output[str] | None = None
    image_repository: str = "mitodl/ol-analytics-api-app"
    min_replicas: int
    web: WorkloadResources = Field(
        default_factory=lambda: WorkloadResources(
            requests={"cpu": "100m", "memory": "256Mi"}, limits={"memory": "512Mi"}
        )
    )
    synced_source: bool = False
    """Source is synced into the running container: Granian reloads on change and
    the root filesystem stays writable so the sync can land."""
    extra_volumes: list[kubernetes.core.v1.VolumeArgs] = Field(default_factory=list)
    extra_volume_mounts: list[kubernetes.core.v1.VolumeMountArgs] = Field(
        default_factory=list
    )


def environment_variables(bindings: OlAnalyticsApiBindings) -> dict[str, Any]:
    """Return the environment, with the k8s labels in ``OTEL_RESOURCE_ATTRIBUTES``."""
    env_vars = dict(bindings.environment_variables)
    vault = bindings.starrocks_vault
    if vault is not None:
        env_vars["OL_ANALYTICS_API_VAULT_ADDR"] = vault.address
        # The Kubernetes auth role is named after the application.
        env_vars["OL_ANALYTICS_API_VAULT_ROLE"] = APPLICATION_NAME
        env_vars["OL_ANALYTICS_API_VAULT_STARROCKS_MOUNT"] = STARROCKS_VAULT_MOUNT
        env_vars["OL_ANALYTICS_API_VAULT_K8S_MOUNT"] = vault.k8s_auth_mount
    # GIT_SHA is baked into the image and pod identity is not injected, so the
    # base OTEL_RESOURCE_ATTRIBUTES must not hold ${GIT_SHA}/${HOSTNAME}
    # placeholders: nothing expands them.
    merge_otel_resource_attributes(env_vars, bindings.k8s_labels)
    return env_vars


def _probe_configs() -> dict[str, kubernetes.core.v1.ProbeArgs]:
    """Return the component's probe paths and timings, on APPLICATION_PORT."""
    return {
        "startup_probe": kubernetes.core.v1.ProbeArgs(
            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                path="/health/startup/",
                port=APPLICATION_PORT,
            ),
            initial_delay_seconds=10,
            period_seconds=10,
            failure_threshold=12,
            success_threshold=1,
            timeout_seconds=5,
        ),
        "readiness_probe": kubernetes.core.v1.ProbeArgs(
            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                path="/health/readiness/",
                port=APPLICATION_PORT,
            ),
            initial_delay_seconds=15,
            period_seconds=15,
            # Matches default_probe_configs: readiness shares the Granian
            # connection budget with user traffic and its failure is correlated
            # across replicas, so a fast eviction empties the EndpointSlice
            # instead of shedding load. See components/services/k8s.py.
            failure_threshold=6,
            timeout_seconds=5,
        ),
        # TCP rather than HTTP so a saturated Granian worker pool cannot fail
        # liveness and trigger a restart that removes capacity from an already
        # overloaded service. Readiness stays on HTTP.
        "liveness_probe": kubernetes.core.v1.ProbeArgs(
            tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(port=APPLICATION_PORT),
            initial_delay_seconds=30,
            period_seconds=30,
            failure_threshold=3,
            timeout_seconds=3,
        ),
    }


def application_config(bindings: OlAnalyticsApiBindings) -> OLApplicationK8sConfig:
    """Return the ``OLApplicationK8s`` config for these bindings."""
    cluster = bindings.cluster
    return OLApplicationK8sConfig(
        project_root=Path(__file__).parent,
        application_config=environment_variables(bindings),
        application_name=APPLICATION_NAME,
        application_namespace=bindings.namespace,
        application_lb_service_name=SERVICE_NAME,
        application_lb_service_port_name=SERVICE_PORT_NAME,
        k8s_global_labels=bindings.k8s_labels,
        env_from_secret_names=bindings.secret_names,
        application_security_group_id=cluster.security_group_id,
        application_security_group_name=cluster.security_group_name,
        application_service_account_name=bindings.service_account_name,
        vault_k8s_resource_auth_name=cluster.vault_auth_name,
        registry=cluster.registry,
        application_image_repository=bindings.image_repository,
        application_docker_tag=bindings.application_docker_tag,
        application_image_digest=bindings.application_image_digest,
        application_version=bindings.application_version,
        application_min_replicas=bindings.min_replicas,
        granian_config=GranianConfig(
            # FastAPI is ASGI; Granian's own default interface is RSGI.
            interface="asgi",
            port=APPLICATION_PORT,
            # Scale on replicas rather than workers per pod: the pod is
            # CPU-request-small and the workload is IO-bound on StarRocks.
            workers=1,
            runtime_threads=1,
            # blocking_threads is deliberately unset: Granian forces it to 1 on
            # the asgi interface and the component rejects an explicit >1.
            no_ws=True,
            # Matches the app's own LOG_LEVEL=INFO, so Granian's lifecycle lines
            # land beside the app's structured output.
            log_level="info",
            application_module="ol_analytics_api.main:app",
            reload=bindings.synced_source,
            # Every reload waits this long for a worker an in-flight request
            # keeps from stopping, so it stays short.
            workers_kill_timeout=1 if bindings.synced_source else None,
        ),
        application_port=APPLICATION_PORT,
        import_nginx_config=False,
        # Not a Django app: no migrations, no collectstatic, no nginx.
        init_migrations=False,
        init_collectstatic=False,
        probe_configs=_probe_configs(),
        resource_requests=bindings.web.requests,
        resource_limits=bindings.web.limits,
        # The image declares `USER appuser` (uid/gid 1000) and
        # PYTHONDONTWRITEBYTECODE=1, so nothing writes to the image filesystem at
        # runtime and a read-only root is free, apart from tempfile's default
        # directory, which the emptyDir below keeps writable.
        pod_security_context=kubernetes.core.v1.PodSecurityContextArgs(
            run_as_non_root=True,
            run_as_user=1000,
            run_as_group=1000,
            # An emptyDir is created root-owned; without an fsGroup the app user
            # cannot write into the /tmp mount below.
            fs_group=1000,
            seccomp_profile=kubernetes.core.v1.SeccompProfileArgs(
                type="RuntimeDefault"
            ),
        ),
        container_security_context=kubernetes.core.v1.SecurityContextArgs(
            allow_privilege_escalation=False,
            read_only_root_filesystem=not bindings.synced_source,
            capabilities=kubernetes.core.v1.CapabilitiesArgs(drop=["ALL"]),
        ),
        extra_volumes=[
            kubernetes.core.v1.VolumeArgs(
                name="tmp",
                empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(
                    size_limit="64Mi"
                ),
            ),
            *bindings.extra_volumes,
        ],
        extra_volume_mounts=[
            kubernetes.core.v1.VolumeMountArgs(
                name="tmp",
                mount_path="/tmp",  # noqa: S108
            ),
            *bindings.extra_volume_mounts,
        ],
        manage_webapp_autoscaler=cluster.autoscalers,
        manage_celery_autoscalers=cluster.autoscalers,
        manage_webapp_memory_vpa=cluster.autoscalers,
        manage_pod_monitor=cluster.pod_monitors,
    )


def hosts(bindings: OlAnalyticsApiBindings) -> list[str]:
    """Return every host the application is served from, canonical first."""
    return [
        host
        for host in (bindings.hostnames.canonical, bindings.hostnames.learn)
        if host is not None
    ]


def cors_allow_origins(bindings: OlAnalyticsApiBindings) -> list[str]:
    """Return the origins allowed to read the API cross-origin with credentials.

    The Learn-scoped host exists because the MIT Learn frontend calls it with
    ``fetch`` from another origin, carrying the shared session cookie, so that
    origin has to be named in ``cors_frontend_origins``.
    """
    canonical = bindings.hostnames.canonical
    return [
        *([f"https://{canonical}"] if canonical is not None else []),
        *bindings.cors_frontend_origins,
    ]


def _canonical_host(bindings: OlAnalyticsApiBindings) -> str:
    canonical = bindings.hostnames.canonical
    if canonical is None:
        msg = "The canonical host's OIDC and routes need hostnames.canonical"
        raise ValueError(msg)
    return canonical


def _learn_host(bindings: OlAnalyticsApiBindings) -> str:
    learn = bindings.hostnames.learn
    if learn is None:
        msg = "The Learn-scoped OIDC and route need hostnames.learn"
        raise ValueError(msg)
    return learn


def canonical_oidc_config(bindings: OlAnalyticsApiBindings) -> OLApisixOIDCConfig:
    """Return the OIDC settings for the canonical host's redirect-to-Keycloak flow.

    The dedicated ``ol-analytics-api-client`` is read from Vault. The component's
    default scope includes ``organization:*``, which the b2b_dashboard tenant's
    auth checks read from X-Userinfo.

    :raises ValueError: Without a canonical host or a Vault auth.
    """
    canonical = _canonical_host(bindings)
    vault_auth_name = bindings.cluster.vault_auth_name
    if vault_auth_name is None:
        msg = "The canonical host's OIDC reads its client from Vault"
        raise ValueError(msg)
    canonical_client = secret_named(
        vault_secrets(), "ol-analytics-api-learner-records-oidc"
    )
    return OLApisixOIDCConfig(
        application_name=APPLICATION_NAME,
        k8s_labels=bindings.k8s_labels,
        k8s_namespace=bindings.namespace,
        oidc_logout_path="/logout/oidc",
        oidc_post_logout_redirect_uri=f"https://{canonical}/",
        oidc_use_session_secret=True,
        # The canonical host's dedicated Keycloak client
        # (substructure/keycloak/olapps.py), the same entry the learner-records
        # tenant reads its issuer and audience from.
        credentials=OLApisixOIDCVaultCredentials(
            mount=canonical_client.mount,
            mount_type=canonical_client.mount_type,
            path=canonical_client.path,
            vaultauth=vault_auth_name,
        ),
    )


def learn_session(bindings: OlAnalyticsApiBindings) -> LearnSession:
    """Return the MIT Learn session the Learn-scoped host shares."""
    return LearnSession.for_host(bindings.env_suffix, _learn_host(bindings))


def learn_oidc_config(bindings: OlAnalyticsApiBindings) -> OLApisixOIDCConfig:
    """Return the OIDC settings for the Learn-scoped host.

    The MIT Learn frontend calls this host with ``fetch``, not a browser
    navigation, so a redirect to Keycloak would surface as a CORS failure rather
    than the JSON 401/403 it expects. Instead of minting its own session this
    resource reads mit-learn's client and cookie (``sso/mitlearn``), as learn-ai's
    /ai/* routes do, so a browser logged in to MIT Learn is recognized here. The
    post-logout landing is mit-learn's /logout/: the session being ended is the
    shared one, and Keycloak rejects ``https://analytics.<env>.learn.mit.edu/``
    for ol-mitlearn-client.
    """
    session = learn_session(bindings)
    return learn_session_oidc_config(
        session,
        OLApisixOIDCRouteGroup(
            hosts=[_learn_host(bindings)],
            post_logout_redirect_uri=session.logout_landing_uri,
        ),
        application_name=f"{APPLICATION_NAME}-learn",
        k8s_labels=bindings.k8s_labels,
        k8s_namespace=bindings.namespace,
        credentials=oidc_credentials(
            vault_auth_name=bindings.cluster.vault_auth_name,
            secret_name=bindings.oidc_secret_name,
            vault_path=LearnSession.vault_path,
        ),
    )


def canonical_route_configs(
    bindings: OlAnalyticsApiBindings,
    oidc: OLApisixOIDCResources,
    *,
    shared_plugin_config_name: str,
) -> list[OLApisixRouteConfig]:
    """Return the learner-records rule and the canonical host's catch-all.

    The learner-records tenant has no browser users: partners call it with a
    client-credentials token from the per-contract clients in
    substructure/keycloak/learner_records.py, so its prefix gets a bearer-only
    rule that answers 401 instead of redirecting. It outranks both catch-alls on
    every host, so a browser session can't reach that tenant through either.

    :param oidc: The resources built from ``canonical_oidc_config``; the
        learner-records rule uses them only for the discovery URL and the
        client_id the audience is checked against.
    """
    bearer_plugin = oidc.get_full_oidc_plugin_config(unauth_action="deny")
    bearer_plugin["config"].update(
        {
            "bearer_only": True,
            # Verify the JWT against Keycloak's JWKS. Introspection reports
            # client-credentials tokens as active: false (same as Opik's SDK
            # route).
            "use_jwks": True,
            "required_scopes": ["learner-records:read"],
            # Every olapps token is signed by the same realm key. Requiring
            # ol-analytics-api-client in `aud`, which the learner-records:read
            # scope adds, keeps tokens minted for other olapps clients out.
            "claim_validator": {
                "audience": {"required": True, "match_with_client_id": True}
            },
            # bearer_token() falls back to X-Access-Token when a request has no
            # Authorization header. The plugin clears an inbound X-Access-Token
            # and only puts the verified token back with this on.
            "set_access_token_header": True,
        }
    )
    return [
        OLApisixRouteConfig(
            route_name="ol-analytics-api-learner-records",
            priority=20,
            shared_plugin_config_name=shared_plugin_config_name,
            plugins=[OLApisixPluginConfig(**bearer_plugin)],
            hosts=hosts(bindings),
            paths=["/api/v1/learner-records", "/api/v1/learner-records/*"],
            **_BACKEND,
        ),
        # Every path behind the openid-connect plugin, so APISIX authenticates
        # the Keycloak session and forwards X-Userinfo; "auth" redirects an
        # unauthenticated browser to Keycloak.
        OLApisixRouteConfig(
            route_name="ol-analytics-api",
            priority=10,
            shared_plugin_config_name=shared_plugin_config_name,
            plugins=[
                OLApisixPluginConfig(
                    **oidc.get_full_oidc_plugin_config(unauth_action="auth")
                ),
            ],
            hosts=[_canonical_host(bindings)],
            paths=["/*"],
            **_BACKEND,
        ),
    ]


def learn_route_config(
    bindings: OlAnalyticsApiBindings,
    oidc: OLApisixOIDCResources,
    *,
    shared_plugin_config_name: str,
) -> OLApisixRouteConfig:
    """Return the Learn-scoped host's catch-all, which never redirects.

    A logged-in browser gets X-Userinfo; an anonymous one reaches the app
    without it and gets a JSON 401.

    :param oidc: The resources built from ``learn_oidc_config``.
    """
    return OLApisixRouteConfig(
        route_name="ol-analytics-api-learn",
        priority=10,
        shared_plugin_config_name=shared_plugin_config_name,
        plugins=[
            OLApisixPluginConfig(
                **oidc.get_full_oidc_plugin_config(unauth_action="pass")
            ),
        ],
        hosts=[_learn_host(bindings)],
        paths=["/*"],
        **_BACKEND,
    )
