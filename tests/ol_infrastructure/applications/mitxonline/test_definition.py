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
from pydantic import ValidationError  # noqa: E402

from ol_infrastructure.applications.mitxonline import definition  # noqa: E402
from ol_infrastructure.components.services.apisix import (  # noqa: E402
    OLApisixOIDCResources,
    OLApisixOIDCVaultCredentials,
)
from ol_infrastructure.components.services.k8s import (  # noqa: E402
    OLApplicationK8s,
    OLApplicationK8sCeleryRedisConfig,
)
from ol_infrastructure.lib.pulumi_helper import docker_image_config_kwargs  # noqa: E402

LOCAL_IMAGE_TAG = "tilt-abc123"
OPENEDX_BASE_URL = "https://lms.mit.test"
LOCAL_OIDC_SECRET = "ol-mitlearn-oidc"  # noqa: S105  # pragma: allowlist secret


def _local_bindings(**overrides) -> definition.MitxonlineBindings:
    """Return bindings shaped like the ones a k3d program would build."""
    settings = {
        "env_suffix": "dev",
        "hostnames": definition.MitxonlineHostnames(
            api="api.mitxonline.mit.test",
            frontend="mitxonline.mit.test",
            learn_api="api.learn.mit.test",
        ),
        "environment_variables": {
            "OPENEDX_API_BASE_URL": OPENEDX_BASE_URL,
            "MITX_ONLINE_ADMIN_EMAIL": "admin@ol.test",
        },
        "k8s_labels": {"ol.mit.edu/application": "mitxonline"},
        "secret_names": ["mitxonline-secrets"],
        "application_docker_tag": LOCAL_IMAGE_TAG,
        "cluster": definition.ClusterCapabilities(
            registry="direct", autoscalers=False, pod_monitors=False
        ),
        "min_replicas": 1,
        "sizing": definition.MitxonlineSizing(
            web=definition.WorkloadResources(
                requests={"cpu": "100m", "memory": "512Mi"}, limits={"memory": "1Gi"}
            )
        ),
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


def test_the_environment_is_the_shared_values_under_the_bindings():
    given = {"OPENEDX_API_BASE_URL": "https://lms.example", "SITE_NAME": "Overridden"}
    env = definition.environment_variables(_local_bindings(environment_variables=given))

    assert env["SITE_NAME"] == "Overridden"
    assert env["USE_X_FORWARDED_HOST"] == "True"
    assert set(env) == {
        *definition.LOCAL_AND_DEPLOYED_ENV,
        *given,
        "OTEL_RESOURCE_ATTRIBUTES",
    }
    assert "ol.mit.edu/application=mitxonline" in env["OTEL_RESOURCE_ATTRIBUTES"]


def test_rendering_does_not_mutate_the_bindings_environment():
    given = {"OPENEDX_API_BASE_URL": "https://lms.example"}
    bindings = _local_bindings(environment_variables=given)

    definition.environment_variables(bindings)

    assert bindings.environment_variables == given


def test_gateway_cors_origins_are_djangos_cors_allowed_origins():
    bindings = _local_bindings(
        environment_variables={
            "OPENEDX_API_BASE_URL": OPENEDX_BASE_URL,
            "CORS_ALLOWED_ORIGINS": "https://mitxonline.mit.dev, https://learn.mit.dev,",
        }
    )

    assert definition.cors_allow_origins(bindings) == [
        "https://mitxonline.mit.dev",
        "https://learn.mit.dev",
    ]


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


def test_release_pipeline_image_kwargs_reach_the_component(monkeypatch):
    """The release pipeline sends a digest and a version; both must get through.

    The bindings forbid unknown fields, so a kwarg docker_image_config_kwargs
    returns that the bindings lack fails every deployed stack.
    """
    digest = "sha256:" + "a" * 64
    monkeypatch.delenv("MITXONLINE_DOCKER_TAG", raising=False)
    monkeypatch.setenv("MITXONLINE_DOCKER_SHA", digest)
    monkeypatch.setenv("MITXONLINE_RELEASE_VERSION", "2026.10.28.1")

    config = definition.application_config(
        _local_bindings(
            application_docker_tag=None, **docker_image_config_kwargs("MITXONLINE")
        )
    )

    assert config.application_image_digest == digest
    assert config.application_version == "2026.10.28.1"


def test_deployed_bindings_keep_the_eks_wiring():
    config = definition.application_config(_deployed_bindings())

    assert config.registry == "ecr"
    assert config.import_nginx_config is False
    assert config.manage_webapp_autoscaler
    assert config.manage_celery_autoscalers
    assert config.manage_webapp_memory_vpa
    assert config.manage_pod_monitor
    assert config.vault_k8s_resource_auth_name == "mitxonline-auth"


def test_bindings_reject_a_misspelled_field():
    with pytest.raises(ValidationError, match="dev_shell_enabled"):
        _local_bindings(dev_shell_enabled=True)


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


def test_the_pre_deploy_job_migrates_then_creates_the_cache_table():
    config = definition.application_config(_deployed_bindings())

    assert config.pre_deploy_commands == [
        (
            "migrate",
            [
                "bash",
                "-c",
                "python manage.py migrate --noinput"
                " && python manage.py createcachetable",
            ],
        ),
    ]


def test_reload_is_off_unless_the_bindings_ask_for_it():
    deployed = definition.application_config(_deployed_bindings())
    local = definition.application_config(_local_bindings(granian_reload=True))

    assert "--reload" not in deployed.granian_config.build_args()
    assert "--workers-kill-timeout" not in deployed.granian_config.build_args()
    assert "--reload" in local.granian_config.build_args()
    assert local.granian_config.workers_kill_timeout == 1
    assert local.granian_config.reload_ignore_dirs == definition.RELOAD_IGNORE_DIRS


def test_sizing_defaults_are_the_deployed_values():
    config = definition.application_config(_deployed_bindings())

    assert config.pre_deploy_resource_requests == {"cpu": "250m", "memory": "3Gi"}
    assert config.pre_deploy_resource_limits == {"memory": "3Gi"}
    assert config.celery_beat_config.resource_requests == {
        "cpu": "10m",
        "memory": "384Mi",
    }
    assert config.celery_beat_config.resource_limits == {"memory": "384Mi"}
    assert {
        w.worker_name: (w.resource_requests, w.resource_limits)
        for w in config.celery_worker_configs
    } == {
        "celery": ({"cpu": "100m", "memory": "2Gi"}, {"memory": "2Gi"}),
        "hubspot_sync": ({"cpu": "100m", "memory": "1Gi"}, {"memory": "1Gi"}),
    }


def test_sizing_reaches_every_workload():
    sizing = definition.MitxonlineSizing(
        web=definition.WorkloadResources(
            requests={"cpu": "11m", "memory": "111Mi"}, limits={"memory": "112Mi"}
        ),
        pre_deploy=definition.WorkloadResources(
            requests={"cpu": "22m", "memory": "222Mi"}, limits={"memory": "223Mi"}
        ),
        celery_beat=definition.WorkloadResources(
            requests={"cpu": "33m", "memory": "333Mi"}, limits={"memory": "334Mi"}
        ),
        celery_queues={
            "celery": definition.WorkloadResources(
                requests={"cpu": "44m", "memory": "256Mi"}, limits={"memory": "256Mi"}
            ),
            "hubspot_sync": definition.WorkloadResources(
                requests={"cpu": "55m", "memory": "768Mi"}, limits={"memory": "768Mi"}
            ),
        },
    )

    config = definition.application_config(_local_bindings(sizing=sizing))

    assert config.resource_requests == sizing.web.requests
    assert config.resource_limits == sizing.web.limits
    assert config.pre_deploy_resource_requests == sizing.pre_deploy.requests
    assert config.pre_deploy_resource_limits == sizing.pre_deploy.limits
    assert config.celery_beat_config.resource_requests == sizing.celery_beat.requests
    assert config.celery_beat_config.resource_limits == sizing.celery_beat.limits
    (worker,) = config.celery_worker_configs
    assert worker.resource_requests == sizing.celery_queues["hubspot_sync"].requests
    assert worker.resource_limits == sizing.celery_queues["hubspot_sync"].limits


def test_merged_worker_takes_an_uncapped_queue():
    size = definition.WorkloadResources(requests={"memory": "1Gi"}, limits={})
    sizing = definition.MitxonlineSizing(
        web=size,
        celery_queues={
            "celery": definition.WorkloadResources(
                requests={"memory": "2Gi"}, limits={"memory": "2Gi"}
            ),
            "hubspot_sync": size,
        },
    )

    (worker,) = definition.celery_worker_configs("merged", sizing)

    assert worker.resource_limits == {}


@pytest.mark.parametrize(
    ("queues", "complaint"),
    [
        (["celery"], r"missing \['hubspot_sync'\]"),
        (["celery", "hubspot_sync", "extra"], r"unknown \['extra'\]"),
    ],
)
def test_sizing_must_name_every_queue_exactly_once(queues, complaint):
    size = definition.WorkloadResources(
        requests={"memory": "1Gi"}, limits={"memory": "1Gi"}
    )

    with pytest.raises(ValidationError, match=complaint):
        definition.MitxonlineSizing(web=size, celery_queues=dict.fromkeys(queues, size))


def test_image_repository_binding_names_the_image():
    config = definition.application_config(
        _local_bindings(image_repository="localhost:5001/mitxonline-app")
    )

    assert config.application_image_repository == "localhost:5001/mitxonline-app"


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
        ("api.mitxonline.mit.test", "mitxonline.mit.test")
    }
    assert {tuple(route.hosts) for route in prefixed} == {("api.learn.mit.test",)}
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
    assert bindings.env_suffix in direct.oidc_session_cookie_name
    assert bindings.env_suffix in prefixed.oidc_session_cookie_name
    assert direct.oidc_session_cookie_domain == ".mitxonline.mit.test"
    assert prefixed.oidc_session_cookie_domain == ".learn.mit.test"


def test_deployed_routes_read_oidc_credentials_from_the_mitlearn_vault_client():
    """Both route groups authenticate through the shared mit-learn Keycloak
    client, whose credentials live at sso/mitlearn in Vault.
    """
    direct = definition.direct_oidc_config(_deployed_bindings())
    prefixed = definition.prefixed_oidc_config(_deployed_bindings())

    expected = OLApisixOIDCVaultCredentials(
        mount="secret-operations",
        mount_type="kv-v1",
        path="sso/mitlearn",
        vaultauth="mitxonline-auth",
    )
    assert direct.credentials == expected
    assert prefixed.credentials == expected


def test_local_routes_read_oidc_credentials_from_the_bound_secret():
    bindings = _local_bindings(oidc_secret_name=LOCAL_OIDC_SECRET)

    direct, prefixed = _route_groups(bindings)

    for route in (direct[0], prefixed[0]):
        [oidc_plugin] = [
            plugin for plugin in route.plugins if plugin.name == "openid-connect"
        ]
        assert oidc_plugin.secret_ref == LOCAL_OIDC_SECRET


@pytest.mark.parametrize(
    "bindings",
    [
        pytest.param(_local_bindings(), id="neither"),
        pytest.param(_deployed_bindings(oidc_secret_name=LOCAL_OIDC_SECRET), id="both"),
    ],
)
def test_oidc_credentials_come_from_exactly_one_source(bindings):
    with pytest.raises(ValueError, match="oidc_secret_name"):
        definition.direct_oidc_config(bindings)


# ─── Open edX login gate ──────────────────────────────────────────────────────


LMS_HOST = "courses.learn.mit.dev"


def _gate_bindings(**overrides) -> definition.MitxonlineBindings:
    return _deployed_bindings(
        openedx_login_gate=definition.OpenedxLoginGate(
            lms_host=LMS_HOST,
            lms_service_host="lms.mitxonline-openedx.svc.cluster.local",
        ),
        **overrides,
    )


def _gate_routes(bindings: definition.MitxonlineBindings):
    oidc = OLApisixOIDCResources(
        "gate-oidc", oidc_config=definition.prefixed_oidc_config(bindings)
    )
    return definition.openedx_login_gate_route_configs(
        bindings, oidc=oidc, shared_plugin_config_name="shared", upstream_name="lms"
    )


def test_openedx_login_gate_renders_nothing_unless_bound():
    assert _gate_routes(_deployed_bindings()) == []


def test_openedx_login_gate_matches_only_browser_page_loads_of_the_login_start():
    """MITx Online replays the Open edX login with python-requests to mint edX
    API tokens; that client sends no Fetch Metadata, so it must not match.
    """
    [route] = _gate_routes(_gate_bindings())

    assert route.hosts == [LMS_HOST]
    assert route.paths == ["/auth/login/ol-oauth2/*"]
    assert {
        (expr["subject"]["name"], expr["op"], expr["value"]) for expr in route.exprs
    } == {
        ("Sec-Fetch-Mode", "Equal", "navigate"),
        ("Sec-Fetch-Dest", "Equal", "document"),
    }


def test_openedx_login_gate_sends_learners_without_a_session_to_keycloak():
    """With unauth_action "pass" the route would forward anonymous learners
    untouched and never write the MIT Learn session.
    """
    [route] = _gate_routes(_gate_bindings())

    [plugin] = [
        plugin
        for plugin in route.plugins
        if (plugin.get("name") if isinstance(plugin, dict) else plugin.name)
        == "openid-connect"
    ]
    config = plugin["config"] if isinstance(plugin, dict) else plugin.config
    assert config["unauth_action"] == "auth"


def test_openedx_login_gate_upstream_passes_the_public_host_to_the_lms():
    """The LMS rejects any Host but its public one (Django DisallowedHost), so
    the upstream must not fall back to the Service's DNS name.
    """
    gate = _gate_bindings().openedx_login_gate
    config = definition.openedx_login_gate_upstream_config(gate, {})

    assert config.external_hostname == "lms.mitxonline-openedx.svc.cluster.local"
    assert config.port == 8000
    assert config.pass_host == "pass"  # noqa: S105
