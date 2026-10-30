"""Tests for the learn-ai application definition."""

from __future__ import annotations

import asyncio

import pulumi

# Python 3.14+ compatibility
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())


class K8sMocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        return [args.name + "_id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


pulumi.runtime.set_mocks(K8sMocks())

import pulumi_kubernetes as kubernetes  # noqa: E402
import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from ol_infrastructure.applications.learn_ai import definition  # noqa: E402
from ol_infrastructure.components.services.apisix import (  # noqa: E402
    OLApisixOIDCResources,
    OLApisixOIDCSecretCredentials,
    OLApisixOIDCVaultCredentials,
    stale_session_cookie_cleanup_plugin,
)
from ol_infrastructure.components.services.k8s import (  # noqa: E402
    OLApplicationK8s,
    OLApplicationK8sCeleryRedisConfig,
)
from ol_infrastructure.lib.ol_types import KubernetesServiceAppProtocol  # noqa: E402
from ol_infrastructure.lib.pulumi_helper import docker_image_config_kwargs  # noqa: E402

LOCAL_IMAGE_TAG = "tilt-abc123"
LOCAL_OIDC_SECRET = "ol-mitlearn-oidc"  # noqa: S105  # pragma: allowlist secret


def _local_bindings(**overrides) -> definition.LearnAiBindings:
    """Return bindings shaped like the ones a k3d program would build."""
    settings = {
        "env_suffix": "dev",
        "hostnames": definition.LearnAiHostnames(
            api="ai.learn.mit.test",
            frontend="ai-sandbox.learn.mit.test",
            learn_api="api.learn.mit.test",
        ),
        "environment_variables": {"MITOL_APP_BASE_URL": "https://ai.learn.mit.test"},
        "cors_frontend_origins": ["https://learn.mit.test"],
        "k8s_labels": {"ol.mit.edu/application": "learn-ai"},
        "secret_names": ["learn-ai-secrets"],
        "application_docker_tag": LOCAL_IMAGE_TAG,
        "cluster": definition.ClusterCapabilities(
            registry="direct", autoscalers=False, pod_monitors=False
        ),
        "min_replicas": 1,
        "sizing": definition.LearnAiSizing(
            web=definition.WorkloadResources(
                requests={"cpu": "100m", "memory": "512Mi"}, limits={"memory": "2Gi"}
            ),
            celery_beat=definition.WorkloadResources(
                requests={"cpu": "10m", "memory": "256Mi"}, limits={"memory": "1Gi"}
            ),
            celery_queues=dict.fromkeys(
                definition.CELERY_QUEUE_NAMES,
                definition.WorkloadResources(
                    requests={"cpu": "50m", "memory": "512Mi"},
                    limits={"memory": "1536Mi"},
                ),
            ),
        ),
        "celery_topology": "merged",
        "oidc_secret_name": LOCAL_OIDC_SECRET,
    }
    settings.update(overrides)
    return definition.LearnAiBindings(**settings)


def _deployed_bindings(**overrides) -> definition.LearnAiBindings:
    """Return bindings shaped like the ones the deployed program builds."""
    settings = {
        "cluster": definition.ClusterCapabilities(
            security_group_id=pulumi.Output.from_input("sg-test"),
            security_group_name=pulumi.Output.from_input("learn-ai-app"),
            vault_auth_name="learn-ai-auth",
        ),
        "service_account_name": pulumi.Output.from_input("learn-ai-admin"),
        "optional_secret_names": ["learn-ai-opik-keycloak"],
        "celery_redis": OLApplicationK8sCeleryRedisConfig(
            host=pulumi.Output.from_input("redis.example.com"),
            password="not-a-real-password",  # pragma: allowlist secret
        ),
        "celery_topology": "per-queue",
        "oidc_secret_name": None,
        "sizing": definition.LearnAiSizing(),
    }
    settings.update(overrides)
    return _local_bindings(**settings)


# ─── Environment ──────────────────────────────────────────────────────────────


def test_the_environment_is_the_shared_values_under_the_bindings():
    env = definition.environment_variables(
        _local_bindings(environment_variables={"A": "1"})
    )

    assert set(env) == {
        *definition.LOCAL_AND_DEPLOYED_ENV,
        "A",
        "OTEL_RESOURCE_ATTRIBUTES",
    }
    assert "ol.mit.edu/application=learn-ai" in env["OTEL_RESOURCE_ATTRIBUTES"]


def test_the_shared_environment_holds_everywhere():
    assert definition.LOCAL_AND_DEPLOYED_ENV == {
        "CANVAS_SYLLABUS_DEMO_READABLE_IDS": [
            "14566-kaleba:20211202",
            "34642-15.095_FA25",
        ],
        "CANVAS_TUTOR_DEMO_RUN_READABLE_IDS": [
            "14566-kaleba:20211202+canvas",
            "34642-15.095_FA25+canvas",
        ],
        "DEBUG": "false",
        "MITOL_FEATURES_DEFAULT": "true",
        "MITOL_SECURE_SSL_REDIRECT": "False",
    }


def test_the_bindings_win_over_the_shared_environment():
    env = definition.environment_variables(
        _local_bindings(environment_variables={"DEBUG": "true"})
    )

    assert env["DEBUG"] == "true"
    assert env["MITOL_FEATURES_DEFAULT"] == "true"


def test_rendering_does_not_mutate_the_bindings_environment():
    given = {"MITOL_APP_BASE_URL": "https://ai.example"}
    bindings = _local_bindings(environment_variables=given)

    definition.environment_variables(bindings)

    assert bindings.environment_variables == given


# ─── Workloads ────────────────────────────────────────────────────────────────


def test_local_bindings_render_without_eks_only_resources():
    app = OLApplicationK8s(definition.application_config(_local_bindings()))

    assert app.security_group_policy is None
    assert app.webapp_autoscaler is None
    assert app.celery_scaled_objects == []
    assert app.webapp_pod_monitor is None
    assert app.beat_deployment is not None


def test_deployed_bindings_keep_the_eks_wiring():
    config = definition.application_config(_deployed_bindings())

    assert config.registry == "ecr"
    assert config.import_nginx_config is False
    assert config.manage_webapp_autoscaler
    assert config.manage_celery_autoscalers
    assert config.manage_webapp_memory_vpa
    assert config.manage_pod_monitor
    assert config.vault_k8s_resource_auth_name == "learn-ai-auth"
    assert config.optional_env_from_secret_names == ["learn-ai-opik-keycloak"]
    assert config.application_lb_service_app_protocol == KubernetesServiceAppProtocol.WS
    assert config.init_migrations
    assert config.init_collectstatic
    assert config.granian_config.interface == "asgi"
    assert config.granian_config.worker_startup_rss == 600


def test_release_pipeline_image_kwargs_reach_the_component(monkeypatch):
    """A kwarg the bindings lack fails every deployed stack; they forbid extras."""
    digest = "sha256:" + "a" * 64
    monkeypatch.delenv("LEARN_AI_DOCKER_TAG", raising=False)
    monkeypatch.setenv("LEARN_AI_DOCKER_SHA", digest)
    monkeypatch.setenv("LEARN_AI_RELEASE_VERSION", "2026.10.28.1")

    config = definition.application_config(
        _local_bindings(
            application_docker_tag=None, **docker_image_config_kwargs("LEARN_AI")
        )
    )

    assert config.application_image_digest == digest
    assert config.application_version == "2026.10.28.1"


def test_bindings_reject_a_misspelled_field():
    with pytest.raises(ValidationError, match="granian_reload"):
        _local_bindings(granian_reload=True)


def test_deployed_topology_runs_one_worker_per_queue():
    workers = definition.application_config(_deployed_bindings()).celery_worker_configs

    assert [w.queue_name for w in workers] == ["default", "edx_content"]


def test_merged_topology_runs_one_worker_for_every_queue():
    (worker,) = definition.application_config(_local_bindings()).celery_worker_configs

    assert worker.queue_name == "default,edx_content"


def test_sizing_defaults_are_the_deployed_values():
    config = definition.application_config(_deployed_bindings())

    assert config.resource_requests == {"cpu": "100m", "memory": "1000Mi"}
    assert config.resource_limits == {"memory": "2048Mi"}
    assert config.celery_beat_config.resource_requests == {
        "cpu": "10m",
        "memory": "1000Mi",
    }
    assert config.celery_beat_config.resource_limits == {"memory": "1536Mi"}
    assert all(
        w.resource_requests == {"cpu": "100m", "memory": "1000Mi"}
        and w.resource_limits == {"memory": "1536Mi"}
        for w in config.celery_worker_configs
    )


def test_sizing_must_name_every_queue():
    size = definition.WorkloadResources(
        requests={"memory": "1Gi"}, limits={"memory": "1Gi"}
    )

    with pytest.raises(ValidationError, match=r"missing \['edx_content'\]"):
        definition.LearnAiSizing(celery_queues={"default": size})


def test_the_bound_volumes_reach_the_component_config():
    volume = kubernetes.core.v1.VolumeArgs(
        name="ca",
        config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
            name="local-dev-ca-bundle"
        ),
    )
    mount = kubernetes.core.v1.VolumeMountArgs(
        name="ca", mount_path="/etc/ssl/local-dev", read_only=True
    )

    config = definition.application_config(
        _local_bindings(extra_volumes=[volume], extra_volume_mounts=[mount])
    )

    assert config.extra_volumes == [volume]
    assert config.extra_volume_mounts == [mount]


# ─── Gateway ──────────────────────────────────────────────────────────────────


def _oidc(bindings, config_fn):
    return OLApisixOIDCResources(
        f"test-{config_fn.__name__}", oidc_config=config_fn(bindings)
    )


def _routes(bindings):
    direct = definition.direct_route_configs(
        bindings,
        _oidc(bindings, definition.direct_oidc_config),
        shared_plugin_config_name="shared",
    )
    prefixed = definition.prefixed_route_configs(
        bindings,
        _oidc(bindings, definition.prefixed_oidc_config),
        shared_plugin_config_name="shared",
    )
    return direct, prefixed


def _plugin_name(plugin):
    return plugin["name"] if isinstance(plugin, dict) else plugin.name


def _plugin_config(plugin):
    return plugin["config"] if isinstance(plugin, dict) else plugin.config


def test_direct_routes_are_the_deployed_six():
    direct, _ = _routes(_local_bindings())

    assert [(r.route_name, r.priority, r.paths) for r in direct] == [
        ("canvas_syllabus_agent", 20, ["/http/canvas_*"]),
        ("passauth", 2, ["/*"]),
        ("logout-redirect", 10, ["/logout/*"]),
        ("reqauth", 10, ["/admin/login/*", "/http/login/*"]),
        ("websocket", 1, ["/ws/*"]),
        ("dnt-policy", 10, ["/.well-known/dnt-policy.txt"]),
    ]
    # OLApisixRouteConfig appends request-id to every route.
    plugin_names = {
        r.route_name: [
            _plugin_name(p) for p in r.plugins if _plugin_name(p) != "request-id"
        ]
        for r in direct
    }
    assert plugin_names["canvas_syllabus_agent"] == ["key-auth"]
    assert plugin_names["logout-redirect"] == ["redirect"]
    assert plugin_names["dnt-policy"] == ["mocking"]
    assert all(r.hosts == ["ai.learn.mit.test"] for r in direct)
    assert [r.route_name for r in direct if r.websocket] == ["websocket"]
    unauth_actions = {
        r.route_name: _plugin_config(p)["unauth_action"]
        for r in direct
        for p in r.plugins
        if _plugin_name(p) == "openid-connect"
    }
    assert unauth_actions == {
        "passauth": "pass",
        "reqauth": "auth",
        "websocket": "pass",
    }


def test_prefixed_routes_sit_under_the_prefix_on_the_learn_host():
    _, prefixed = _routes(_local_bindings())

    assert [r.route_name for r in prefixed] == [
        "canvas_syllabus_agent",
        "passauth",
        "logout-redirect",
        "reqauth",
        "websocket",
    ]
    assert {r.route_name: r.priority for r in prefixed} == {
        "canvas_syllabus_agent": 20,
        "passauth": 2,
        "logout-redirect": 10,
        "reqauth": 10,
        "websocket": 1,
    }
    assert all(r.hosts == ["api.learn.mit.test"] for r in prefixed)
    assert all(path.startswith("/ai/") for r in prefixed for path in r.paths)
    assert [r.route_name for r in prefixed if r.websocket] == ["websocket"]


def test_the_prefix_is_stripped_before_the_upstream():
    _, prefixed = _routes(_local_bindings())

    for name in ("passauth", "reqauth", "websocket"):
        (route,) = [r for r in prefixed if r.route_name == name]
        first = route.plugins[0]
        assert _plugin_name(first) == "proxy-rewrite"
        assert _plugin_config(first)["regex_uri"] == ["/ai/(.*)", "/$1"]


def test_session_cookies_are_named_for_the_environment():
    b = _local_bindings()

    assert (
        definition.direct_oidc_config(b).oidc_session_cookie_name
        == "learn_ai_apisix_session_dev"
    )
    assert (
        definition.prefixed_oidc_config(b).oidc_session_cookie_name
        == "mitlearn_apisix_session_dev"
    )
    assert (
        definition.prefixed_oidc_config(b).oidc_session_cookie_domain
        == ".learn.mit.test"
    )
    assert definition.direct_oidc_config(b).oidc_session_cookie_domain is None


def test_the_direct_host_keeps_its_own_logout_and_scope():
    config = definition.direct_oidc_config(_local_bindings())

    assert (
        config.oidc_logout_path,
        config.oidc_post_logout_redirect_uri,
        config.oidc_scope,
    ) == ("/logout", "/", "openid profile email")


def test_the_prefixed_group_logs_out_through_learn():
    config = definition.prefixed_oidc_config(_local_bindings())

    assert config.oidc_logout_path == "/ai/logout/oidc"
    assert config.oidc_post_logout_redirect_uri == "https://api.learn.mit.test/logout/"


def test_deployed_routes_read_oidc_credentials_from_the_mitlearn_vault_client():
    for config_fn in (definition.direct_oidc_config, definition.prefixed_oidc_config):
        creds = config_fn(_deployed_bindings()).credentials

        assert isinstance(creds, OLApisixOIDCVaultCredentials)
        assert (creds.path, creds.vaultauth) == ("sso/mitlearn", "learn-ai-auth")


def test_local_routes_read_oidc_credentials_from_the_bound_secret():
    for config_fn in (definition.direct_oidc_config, definition.prefixed_oidc_config):
        creds = config_fn(_local_bindings()).credentials

        assert isinstance(creds, OLApisixOIDCSecretCredentials)
        assert creds.secret_name == LOCAL_OIDC_SECRET


def test_oidc_credentials_must_come_from_some_source():
    with pytest.raises(ValueError, match="oidc_secret_name"):
        definition.direct_oidc_config(_local_bindings(oidc_secret_name=None))


def test_gateway_cors_origins_are_the_three_hosts_plus_the_frontends():
    assert definition.cors_allow_origins(_local_bindings()) == [
        "https://ai-sandbox.learn.mit.test",
        "https://ai.learn.mit.test",
        "https://api.learn.mit.test",
        "https://learn.mit.test",
    ]


def test_shared_plugins_evict_the_stale_default_cookie():
    expected = stale_session_cookie_cleanup_plugin()

    plugins = definition.shared_plugins(_local_bindings())

    assert [p.name for p in plugins] == [expected.name]
    assert [p.config for p in plugins] == [expected.config]


def test_the_canvas_consumer_carries_the_key():
    spec = definition.canvas_agent_consumer_spec("the-key")

    assert spec["authParameter"]["keyAuth"]["value"]["key"] == "the-key"
    assert spec["ingressClassName"] == "apache-apisix"
