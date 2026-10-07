"""Tests for the MITx Online application definition.

The definition is called by the deployed program and by a local one. These
tests render it with bindings shaped like a local k3d cluster, which has no
AWS, Vault, KEDA, VPA or Prometheus operator, and pin what the bindings are
allowed to change.
"""

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

import pytest  # noqa: E402

from ol_infrastructure.applications.mitxonline import definition  # noqa: E402
from ol_infrastructure.components.services.apisix import (  # noqa: E402
    OLApisixOIDCResources,
)
from ol_infrastructure.components.services.k8s import (  # noqa: E402
    OLApplicationK8s,
    OLApplicationK8sCeleryRedisConfig,
)

LOCAL_IMAGE_TAG = "tilt-abc123"
OPENEDX_BASE_URL = "https://lms.mit.dev"


def _local_bindings(**overrides) -> definition.MitxonlineBindings:
    """Return bindings shaped like the ones a k3d program would build."""
    settings = {
        "env_suffix": "dev",
        "hostnames": definition.MitxonlineHostnames(
            api="api.mitxonline.mit.dev",
            frontend="mitxonline.mit.dev",
            learn_api="api.learn.mit.dev",
        ),
        "environment_variables": {
            "OPENEDX_API_BASE_URL": OPENEDX_BASE_URL,
            "MITX_ONLINE_ADMIN_EMAIL": "admin@odl.local",
        },
        "k8s_labels": {"ol.mit.edu/application": "mitxonline"},
        "secret_names": ["mitxonline-secrets"],
        "application_docker_tag": LOCAL_IMAGE_TAG,
        "cluster": definition.ClusterCapabilities(
            registry="direct", autoscalers=False, pod_monitors=False
        ),
        "min_replicas": 1,
        "web_memory_limit": "1Gi",
        "celery_topology": "merged",
    }
    settings.update(overrides)
    return definition.MitxonlineBindings(**settings)


def _deployed_bindings(**overrides) -> definition.MitxonlineBindings:
    """Return bindings shaped like the ones the deployed program builds."""
    settings = {
        "cluster": definition.ClusterCapabilities(
            security_group_id=pulumi.Output.from_input("sg-test"),
            security_group_name=pulumi.Output.from_input("mitxonline-sg"),
            vault_auth_name="mitxonline-auth",
        ),
        "celery_redis": OLApplicationK8sCeleryRedisConfig(
            host=pulumi.Output.from_input("redis.example.com"),
            password="not-a-real-password",  # pragma: allowlist secret
        ),
        "celery_topology": "per-queue",
    }
    settings.update(overrides)
    return _local_bindings(**settings)


# ─── Environment ──────────────────────────────────────────────────────────────


def test_bindings_override_the_shared_environment():
    env = definition.environment_variables(_local_bindings())

    assert env["MITX_ONLINE_ADMIN_EMAIL"] == "admin@odl.local"
    assert env["SITE_NAME"] == definition.ENVIRONMENT_VARIABLES["SITE_NAME"]
    assert "ol.mit.edu/application=mitxonline" in env["OTEL_RESOURCE_ATTRIBUTES"]


def test_rendering_does_not_mutate_the_shared_environment():
    before = dict(definition.ENVIRONMENT_VARIABLES)

    definition.environment_variables(_local_bindings())

    assert before == definition.ENVIRONMENT_VARIABLES


# ─── Workloads ────────────────────────────────────────────────────────────────


def test_local_bindings_render_without_eks_only_resources():
    app = OLApplicationK8s(definition.application_config(_local_bindings()))

    assert app.security_group_policy is None
    assert app.webapp_autoscaler is None
    assert app.celery_scaled_objects == []
    assert app.webapp_pod_monitor is None
    assert app.beat_deployment is not None


def test_local_bindings_use_the_image_as_given():
    config = definition.application_config(_local_bindings())

    assert config.registry == "direct"
    assert config.application_image_repository == "mitodl/mitxonline-app"
    assert config.application_docker_tag == LOCAL_IMAGE_TAG


def test_deployed_topology_runs_one_worker_per_queue():
    config = definition.application_config(_deployed_bindings())

    assert [(w.worker_name, w.queue_name) for w in config.celery_worker_configs] == [
        ("celery", "celery"),
        ("hubspot_sync", "hubspot_sync"),
    ]


def test_merged_topology_runs_one_worker_for_every_queue():
    config = definition.application_config(_local_bindings())

    assert [(w.worker_name, w.queue_name) for w in config.celery_worker_configs] == [
        ("all", "celery,hubspot_sync"),
    ]


def test_reload_is_off_unless_the_bindings_ask_for_it():
    deployed = definition.application_config(_deployed_bindings())
    local = definition.application_config(_local_bindings(granian_reload=True))

    assert "--reload" not in deployed.granian_config.build_args()
    assert "--workers-kill-timeout" not in deployed.granian_config.build_args()
    assert "--reload" in local.granian_config.build_args()
    assert local.granian_config.reload_ignore_dirs == definition.RELOAD_IGNORE_DIRS


# ─── Routes ───────────────────────────────────────────────────────────────────


def _route_groups(bindings: definition.MitxonlineBindings):
    direct = definition.direct_route_configs(
        bindings,
        oidc=OLApisixOIDCResources(
            "direct-oidc", oidc_config=definition.direct_oidc_config(bindings)
        ),
        shared_plugin_config_name="shared",
    )
    prefixed = definition.prefixed_route_configs(
        bindings,
        oidc=OLApisixOIDCResources(
            "prefixed-oidc", oidc_config=definition.prefixed_oidc_config(bindings)
        ),
        shared_plugin_config_name="shared",
    )
    return direct, prefixed


def test_routes_follow_the_bound_hostnames():
    direct, prefixed = _route_groups(_deployed_bindings())

    assert {tuple(route.hosts) for route in direct} == {
        ("api.mitxonline.mit.dev", "mitxonline.mit.dev")
    }
    assert {tuple(route.hosts) for route in prefixed} == {("api.learn.mit.dev",)}
    assert [route.route_name for route in direct] == [
        "passauth",
        "logout-redirect",
        "reqauth",
        "cart",
        "static-hash",
        "dnt-policy",
    ]
    assert [route.route_name for route in prefixed] == [
        "passauth",
        "logout-redirect",
        "reqauth",
        "static-hash",
    ]


def test_routes_point_at_the_service_the_component_creates():
    bindings = _deployed_bindings()
    config = definition.application_config(bindings)
    direct, prefixed = _route_groups(bindings)

    for route in [*direct, *prefixed]:
        assert route.backend_service_name == config.application_lb_service_name
        assert route.backend_service_port == config.application_lb_service_port_name


def test_frame_ancestors_header_names_the_bound_openedx_host():
    direct, _ = _route_groups(_deployed_bindings())
    rewrites = [
        plugin
        for plugin in direct[0].plugins
        if getattr(plugin, "name", None) == "response-rewrite"
    ]

    assert rewrites[0].config["headers"]["set"]["Content-Security-Policy"] == (
        f"frame-ancestors 'self' {OPENEDX_BASE_URL}"
    )


def test_routes_refuse_bindings_without_an_openedx_host():
    bindings = _deployed_bindings(environment_variables={})

    with pytest.raises(KeyError, match="OPENEDX_API_BASE_URL"):
        _route_groups(bindings)


def test_session_cookies_are_named_for_the_environment():
    bindings = _deployed_bindings()

    direct = definition.direct_oidc_config(bindings)
    prefixed = definition.prefixed_oidc_config(bindings)

    assert direct.oidc_session_cookie_name != prefixed.oidc_session_cookie_name
    assert direct.oidc_session_cookie_domain == ".mitxonline.mit.dev"
    assert prefixed.oidc_session_cookie_domain == ".learn.mit.dev"


def test_oidc_needs_vault_until_it_can_read_a_plain_secret():
    with pytest.raises(ValueError, match="vault_auth_name"):
        definition.direct_oidc_config(_local_bindings())
