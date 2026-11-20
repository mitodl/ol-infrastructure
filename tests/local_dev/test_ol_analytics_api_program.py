"""Render the local ol-analytics-api program under mocks; pin what matches deployed."""

import functools
import importlib
import importlib.util
import json

import pytest
import yaml

from ol_infrastructure.applications.ol_analytics_api import definition, vault_secrets
from ol_infrastructure.lib.local_dev_program import CA_BUNDLE_DIR
from ol_infrastructure.lib.secret_env import VaultSecret
from tests.local_dev.program_render import (
    APP_COMPONENT,
    DEPLOYMENT,
    JOB,
    OIDC_SECRET,
    PLUGIN_CONFIG,
    REPO,
    ROUTE,
    SERVICE,
    literal_env,
    render_program,
    secret_string_data,
)
from tests.local_dev.program_render import one as _one

APP_DIR = REPO / "local-dev/apps/ol-analytics-api"
NAMESPACE = "ol-analytics-api"
WEBAPP = "ol-analytics-api-app"
ROUTE_NAME = "ol-analytics-api-dev-route"
SECRET_VALUE = "s3cret-override"  # noqa: S105  # pragma: allowlist secret

_render = functools.partial(render_program, "ol-analytics-api")


def _mitxonline_inputs():
    spec = importlib.util.spec_from_file_location(
        "mitxonline_inputs", REPO / "local-dev/apps/mitxonline/pulumi/inputs.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_secret_data = functools.partial(secret_string_data, name="ol-analytics-api-secrets")


def _container(mocks):
    return _one(mocks, DEPLOYMENT, WEBAPP)["spec"]["template"]["spec"]["containers"][0]


def _env(mocks):
    return literal_env(_one(mocks, DEPLOYMENT, WEBAPP))


def _rule(mocks):
    (rule,) = _one(mocks, ROUTE, ROUTE_NAME)["spec"]["http"]
    return rule


def _oidc_plugin(mocks):
    return {p["name"]: p for p in _rule(mocks)["plugins"]}["openid-connect"]


def test_the_workload_is_the_deployed_shape_without_a_readiness_wait(
    tmp_path, monkeypatch
):
    mocks = _render(tmp_path, monkeypatch)
    deployments = [i for t, _, i in mocks.resources if t == DEPLOYMENT]
    assert [d["metadata"]["name"] for d in deployments] == [WEBAPP]
    assert {d["metadata"]["namespace"] for d in deployments} == {NAMESPACE}
    containers = deployments[0]["spec"]["template"]["spec"]["containers"]
    assert [c["name"] for c in containers] == [WEBAPP]
    assert containers[0]["ports"][0]["containerPort"] == definition.APPLICATION_PORT
    service = _one(mocks, SERVICE, definition.SERVICE_NAME)
    assert service["spec"]["ports"][0]["port"] == definition.APPLICATION_PORT
    # The definition runs no migrations, so there is nothing to wait on.
    assert not [i for t, _, i in mocks.resources if t == JOB]
    (transform,) = mocks.transforms[APP_COMPONENT]
    assert getattr(transform, "func", transform).__name__ == "await_policy"


def test_probes_and_pod_security_are_the_definitions(tmp_path, monkeypatch):
    mocks = _render(tmp_path, monkeypatch)
    pod = _one(mocks, DEPLOYMENT, WEBAPP)["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["startupProbe"]["httpGet"]["path"] == "/health/startup/"
    assert container["readinessProbe"]["httpGet"]["path"] == "/health/readiness/"
    assert int(container["livenessProbe"]["tcpSocket"]["port"]) == 8000
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert int(pod["securityContext"]["fsGroup"]) == 1000
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    mounts = {m["mountPath"] for m in container["volumeMounts"]}
    assert {"/tmp", CA_BUNDLE_DIR} <= mounts  # noqa: S108


def test_the_pod_is_sized_as_deployed_and_the_limit_is_stack_config(
    tmp_path, monkeypatch
):
    resources = _container(_render(tmp_path / "a", monkeypatch))["resources"]
    assert resources == {
        "requests": {"cpu": "100m", "memory": "256Mi"},
        "limits": {"memory": "512Mi"},
    }
    resources = _container(
        _render(
            tmp_path / "b",
            monkeypatch,
            config={"ol_analytics_api:web_memory_limit": "1Gi"},
        )
    )["resources"]
    assert resources["limits"] == {"memory": "1Gi"}
    assert resources["requests"]["memory"] == "256Mi"


def test_the_container_reads_the_ca_bundle_the_core_stack_writes(tmp_path, monkeypatch):
    """SSL_CERT_FILE names a file in the core stack's bundle ConfigMap."""
    monkeypatch.syspath_prepend(str(REPO / "local-dev/infra"))
    tls = importlib.import_module("modules.tls")
    mocks = _render(tmp_path, monkeypatch)
    env = _env(mocks)
    expected = f"{CA_BUNDLE_DIR}/{tls.CA_BUNDLE_FILENAME}"
    assert env["SSL_CERT_FILE"] == expected
    assert env["REQUESTS_CA_BUNDLE"] == expected
    pod = _one(mocks, DEPLOYMENT, WEBAPP)["spec"]["template"]["spec"]
    volumes = {
        v["name"]: v["configMap"]["name"] for v in pod["volumes"] if "configMap" in v
    }
    (mount,) = [
        m for m in _container(mocks)["volumeMounts"] if m["mountPath"] == CA_BUNDLE_DIR
    ]
    assert volumes[mount["name"]] == tls.CA_BUNDLE_CONFIGMAP


# Keys from outside the local layers: PORT from the component,
# OTEL_RESOURCE_ATTRIBUTES from the definition's label merge. Grow this only with
# a name you found in src/ (grep it); anything else here is a deployed value
# leaking.
COMPONENT_ENV = {"PORT", "OTEL_RESOURCE_ATTRIBUTES"}


def test_env_is_exactly_the_local_layers_with_no_vault_login(tmp_path, monkeypatch):
    """With no OL_ANALYTICS_API_VAULT_* the app logs in to StarRocks as root."""
    mocks = _render(
        tmp_path,
        monkeypatch,
        config={"ol_analytics_api:env_vars": {"NEW_KEY": "1"}},
    )
    env = _env(mocks)
    assert set(env) == (
        set(mocks.program["LOCAL_ENV_DEFAULTS"]) | {"NEW_KEY"} | COMPONENT_ENV
    )
    assert not [name for name in env if name.startswith("OL_ANALYTICS_API_VAULT_")]
    assert env["STARROCKS_USER"] == "root"
    assert env["STARROCKS_PASSWORD"] == ""


def test_the_root_domain_flows_into_hosts_env_and_the_secret(tmp_path, monkeypatch):
    mocks = _render(tmp_path, monkeypatch, root_domain="mit.localhost")
    bindings = mocks.program["bindings"]
    assert bindings.hostnames.canonical is None
    assert bindings.hostnames.learn == "analytics.learn.mit.localhost"
    env = _env(mocks)
    assert env["OL_ANALYTICS_API_B2B_DASHBOARD_MITXONLINE_API_BASE_URL"] == (
        "https://api.mitxonline.mit.localhost"
    )
    secret = _secret_data(mocks)
    assert secret["OL_ANALYTICS_API_B2B_LEARNER_RECORDS_ISSUER"] == (
        "https://sso.ol.mit.localhost/realms/olapps"
    )
    rendered = json.dumps(
        [env, secret]
        + [i for t, _, i in mocks.resources if t in {ROUTE, PLUGIN_CONFIG}],
        default=str,
    )
    assert "mit.test" not in rendered


def test_the_secret_is_keyed_by_the_deployed_vault_variables(tmp_path, monkeypatch):
    """Locally there is no Vault: one plain Secret stands in for the VSO resources.

    Its keys are the deployed description's variable names, less those
    secrets.yaml leaves unset so the app's default applies.
    """
    mocks = _render(tmp_path, monkeypatch)
    local = yaml.safe_load((APP_DIR / "secrets.yaml").read_text())
    deployed = vault_secrets.secret_env_names()
    assert set(local["unset"]) <= set(deployed)
    # A set: the mock receives the data as a protobuf map, which keeps no order.
    assert set(_secret_data(mocks)) == set(deployed) - set(local["unset"])
    env_from = _container(mocks)["envFrom"]
    assert {"secretRef": {"name": "ol-analytics-api-secrets"}} in env_from


def test_the_secret_presents_the_client_pair_mitxonline_registers(
    tmp_path, monkeypatch
):
    """A drift between the two fails every org-manager check with a 502."""
    inputs = _mitxonline_inputs()
    secret = _secret_data(_render(tmp_path, monkeypatch))
    assert secret[vault_secrets.MITXONLINE_CLIENT_ID] == inputs.ANALYTICS_API_CLIENT_ID
    assert secret[vault_secrets.MITXONLINE_CLIENT_SECRET] == (
        inputs.ANALYTICS_API_CLIENT_SECRET
    )


def test_the_client_pair_in_secrets_yaml_stops_the_program(tmp_path, monkeypatch):
    """The pair comes from mitxonline's inputs only, so it cannot drift."""
    secrets = (APP_DIR / "secrets.yaml").read_text()
    edited = secrets.replace(
        "unset:\n", f"unset:\n- {vault_secrets.MITXONLINE_CLIENT_ID}\n", 1
    )
    assert edited != secrets
    with pytest.raises(
        ValueError, match=f"listed twice: {vault_secrets.MITXONLINE_CLIENT_ID};"
    ):
        _render(tmp_path, monkeypatch, app_files={"secrets.yaml": edited})


def test_a_variable_added_for_deployed_stacks_stops_the_local_program(
    tmp_path, monkeypatch
):
    """The drift check: a new deployed variable needs a value or an unset entry."""
    original = vault_secrets.vault_secrets

    def with_new_variable():
        return [
            *original(),
            VaultSecret(
                name="new",
                kind="static",
                mount="secret-operations",
                mount_type="kv-v1",
                path="new",
                env={"NEW_DEPLOYED_VARIABLE": ("value",)},
            ),
        ]

    monkeypatch.setattr(vault_secrets, "vault_secrets", with_new_variable)
    with pytest.raises(ValueError, match="missing: NEW_DEPLOYED_VARIABLE;"):
        _render(tmp_path, monkeypatch)


def test_a_secrets_edit_rolls_the_pod(tmp_path, monkeypatch):
    def config_hash(mocks):
        template = _one(mocks, DEPLOYMENT, WEBAPP)["spec"]["template"]
        return template["metadata"]["annotations"]["ol.mit.edu/config-hash"]

    before = config_hash(_render(tmp_path / "a", monkeypatch))
    secrets = (APP_DIR / "secrets.yaml").read_text()
    edited = secrets.replace("/realms/olapps", "/realms/other")
    assert edited != secrets
    after = config_hash(
        _render(tmp_path / "b", monkeypatch, app_files={"secrets.yaml": edited})
    )
    assert before != after


def test_the_image_tilt_built_reloads_and_the_published_one_does_not(
    tmp_path, monkeypatch
):
    """Tilt syncs src/ into the image it built, so that one reloads and keeps a
    writable root filesystem; the published image runs as deployed.
    """
    built = "k3d-registry.localhost:5000/mitodl_ol-analytics-api-app:tilt-feedface"
    container = _container(
        _render(tmp_path / "built", monkeypatch, environ={"TILT_IMAGE_0": built})
    )
    assert container["image"] == built
    assert "--reload" in container["args"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is False
    container = _container(_render(tmp_path / "published", monkeypatch))
    assert container["image"] == "mitodl/ol-analytics-api-app:latest"
    assert "--reload" not in container["args"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True


def test_every_rendered_object_carries_the_deployed_labels_and_managed_by(
    tmp_path, monkeypatch
):
    mocks = _render(tmp_path, monkeypatch)
    rendered = [
        (t, i) for t, _, i in mocks.resources if t.startswith("kubernetes:") and i
    ]
    # The Deployment, Service, Secret, route and plugin config at least.
    assert len(rendered) >= 5
    for typ, inputs in rendered:
        labels = inputs["metadata"]["labels"]
        assert labels["ol.mit.edu/managed-by"] == "local-dev-pulumi", typ
        assert labels["ol.mit.edu/ou"] == "data", typ
        assert labels["ol.mit.edu/service"] == "ol-analytics-api", typ
        assert inputs["metadata"]["namespace"] == NAMESPACE, typ


def test_only_the_learn_scoped_rule_is_rendered(tmp_path, monkeypatch):
    """The canonical host and the bearer-only learner-records rule are not local."""
    mocks = _render(tmp_path, monkeypatch)
    routes = [i for t, _, i in mocks.resources if t == ROUTE]
    assert [r["metadata"]["name"] for r in routes] == [ROUTE_NAME]
    rule = _rule(mocks)
    assert rule["name"] == "ol-analytics-api-learn"
    assert rule["match"]["hosts"] == ["analytics.learn.mit.test"]
    assert rule["match"]["paths"] == ["/*"]
    assert rule["backends"][0]["serviceName"] == definition.SERVICE_NAME
    assert rule["backends"][0]["servicePort"] == definition.SERVICE_PORT_NAME


def test_the_route_shares_mit_learns_session_and_never_redirects(tmp_path, monkeypatch):
    plugin = _oidc_plugin(_render(tmp_path, monkeypatch))
    assert plugin["secretRef"] == OIDC_SECRET
    config = plugin["config"]
    assert config["unauth_action"] == "pass"
    assert config.get("ssl_verify", True) is True
    assert config["session"]["cookie_name"] == "mitlearn_apisix_session_dev"
    assert config["session"]["cookie_domain"] == ".learn.mit.test"
    # The org membership claim the b2b_dashboard tenant reads.
    assert "organization:*" in config["scope"].split()


def test_shared_plugins_allow_learn_and_omit_opentelemetry(tmp_path, monkeypatch):
    mocks = _render(tmp_path, monkeypatch)
    config = _one(mocks, PLUGIN_CONFIG, "ol-analytics-api-ol-shared-plugins")
    plugins = {p["name"]: p for p in config["spec"]["plugins"]}
    assert "opentelemetry" not in plugins
    assert {"redirect", "cors", "prometheus"} <= set(plugins)
    assert plugins["cors"]["config"]["allow_origins"] == "https://learn.mit.test"
    assert _rule(mocks)["plugin_config_name"] == "ol-analytics-api-ol-shared-plugins"


def test_the_overrides_replace_a_field_the_definition_leaves_empty(
    tmp_path, monkeypatch
):
    """model_copy replaces config_hash_inputs wholesale."""
    bindings = _render(tmp_path, monkeypatch).program["bindings"]
    assert definition.application_config(bindings).config_hash_inputs == {}
