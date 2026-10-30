"""Render the local learn-ai program under mocks; pin what must match deployed."""

import functools
import importlib
import json

import pulumi
import pytest

from ol_infrastructure.applications.learn_ai import definition
from ol_infrastructure.lib.local_dev_program import CA_BUNDLE_DIR
from tests.local_dev.program_render import (
    APP_COMPONENT,
    DEPLOYMENT,
    JOB,
    OIDC_SECRET,
    PLUGIN_CONFIG,
    REPO,
    ROUTE,
    SECRET_TYPE,
    SERVICE,
    render_program,
)
from tests.local_dev.program_render import one as _one

APP_DIR = REPO / "local-dev/apps/learn-ai"
CONSUMER = "kubernetes:apisix.apache.org/v2:ApisixConsumer"
KEY_VALUE = "sk-secret-value"  # pragma: allowlist secret
MIGRATE_COMMAND = [
    "bash",
    "-c",
    "python manage.py migrate --noinput && python manage.py createcachetable",
]

_render = functools.partial(render_program, "learn-ai")


def _secret_data(mocks):
    """Return the local Secret's stringData, which the provider marks secret.

    The mock sees the marked value as Pulumi's wire form, a dict with the
    secret signature key beside the value.
    """
    data = _one(mocks, SECRET_TYPE, "learn-ai-secrets")["stringData"]
    return data["value"] if pulumi.runtime.rpc._special_sig_key in data else data


def _plugin(route_inputs, rule, plugin):
    rules = {r["name"]: r for r in route_inputs["spec"]["http"]}
    return {p["name"]: p for p in rules[rule]["plugins"]}[plugin]


def _env(deployment):
    """Literal env values.

    valueFrom entries (downward API) are the component's, not config the app reads.
    """
    return {
        e["name"]: e["value"]
        for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in e
    }


def _resources(workload):
    return workload["spec"]["template"]["spec"]["containers"][0]["resources"]


def _await_result(mocks, typ, inputs):
    (transform,) = mocks.transforms[APP_COMPONENT]
    return transform(
        pulumi.ResourceTransformArgs(
            custom=True,
            type_=typ,
            name="x",
            props=inputs,
            opts=pulumi.ResourceOptions(),
        )
    )


def test_workloads_are_the_deployed_shape_without_a_readiness_wait(
    tmp_path, monkeypatch
):
    mocks = _render(tmp_path, monkeypatch)
    webapp = _one(mocks, DEPLOYMENT, "learn-ai-app")
    containers = webapp["spec"]["template"]["spec"]["containers"]
    assert [c["name"] for c in containers] == ["learn-ai-app"]
    assert containers[0]["ports"][0]["containerPort"] == 8073
    service = _one(mocks, SERVICE, "learn-ai-webapp")
    assert service["spec"]["ports"][0]["port"] == 8073
    assert {
        i["metadata"]["name"] for t, _, i in mocks.resources if t == DEPLOYMENT
    } == {
        "learn-ai-app",
        "learn-ai-all-celery-worker",
        "learn-ai-celery-beat",
    }
    # await_policy itself is covered in test_local_dev_program.py; here, that the
    # component gets it.
    (transform,) = mocks.transforms[APP_COMPONENT]
    assert getattr(transform, "func", transform).__name__ == "await_policy"


def test_workloads_are_laptop_sized(tmp_path, monkeypatch):
    mocks = _render(tmp_path, monkeypatch)
    assert _resources(_one(mocks, DEPLOYMENT, "learn-ai-app")) == {
        "requests": {"cpu": "100m", "memory": "512Mi"},
        "limits": {"memory": "2Gi"},
    }
    assert _resources(_one(mocks, JOB, "learn-ai-app-pre-deploy")) == {
        "requests": {"cpu": "100m", "memory": "512Mi"},
        "limits": {"memory": "1Gi"},
    }
    assert _resources(_one(mocks, DEPLOYMENT, "learn-ai-all-celery-worker")) == {
        "requests": {"cpu": "50m", "memory": "512Mi"},
        "limits": {"memory": "1536Mi"},
    }
    assert _resources(_one(mocks, DEPLOYMENT, "learn-ai-celery-beat")) == {
        "requests": {"cpu": "10m", "memory": "256Mi"},
        "limits": {"memory": "1Gi"},
    }


def test_the_stack_file_sets_memory_limits_and_leaves_requests(tmp_path, monkeypatch):
    mocks = _render(
        tmp_path,
        monkeypatch,
        config={
            "learn_ai:web_memory_limit": "3Gi",
            "learn_ai:worker_memory_limit": "2Gi",
            "learn_ai:beat_memory_limit": "768Mi",
            "learn_ai:pre_deploy_memory_limit": "2Gi",
        },
    )
    expected = {
        (DEPLOYMENT, "learn-ai-app"): ("3Gi", "512Mi"),
        (DEPLOYMENT, "learn-ai-all-celery-worker"): ("2Gi", "512Mi"),
        (JOB, "learn-ai-app-pre-deploy"): ("2Gi", "512Mi"),
        (DEPLOYMENT, "learn-ai-celery-beat"): ("768Mi", "256Mi"),
    }
    for (typ, name), (limit, request) in expected.items():
        resources = _resources(_one(mocks, typ, name))
        assert resources["limits"] == {"memory": limit}, name
        assert resources["requests"]["memory"] == request, name


def test_the_pre_deploy_job_migrates_then_creates_the_cache_table(
    tmp_path, monkeypatch
):
    mocks = _render(tmp_path, monkeypatch)
    job = _one(mocks, JOB, "learn-ai-app-pre-deploy")
    (container,) = job["spec"]["template"]["spec"]["containers"]
    assert container["command"] == MIGRATE_COMMAND
    # The definition's migrate and collectstatic init containers run in the
    # webapp pod as well.
    webapp = _one(mocks, DEPLOYMENT, "learn-ai-app")
    init_names = [
        c["name"] for c in webapp["spec"]["template"]["spec"]["initContainers"]
    ]
    assert {"migrate", "collectstatic"} <= set(init_names)


def test_every_container_mounts_each_path_once(tmp_path, monkeypatch):
    """The component adds extra_volume_mounts to every init container and to the Job.

    A second mount of the same path is rejected by the API server.
    """
    mocks = _render(tmp_path, monkeypatch)
    for typ in (DEPLOYMENT, JOB):
        for _, name, inputs in [(t, n, i) for t, n, i in mocks.resources if t == typ]:
            spec = inputs["spec"]["template"]["spec"]
            for container in [*(spec.get("initContainers") or []), *spec["containers"]]:
                paths = [m["mountPath"] for m in container.get("volumeMounts") or []]
                assert len(paths) == len(set(paths)), (
                    f"{name}/{container['name']}: {paths}"
                )


def test_every_container_reads_the_ca_bundle_the_core_stack_writes(
    tmp_path, monkeypatch
):
    """SSL_CERT_FILE names a file in the core stack's bundle ConfigMap."""
    monkeypatch.syspath_prepend(str(REPO / "local-dev/infra"))
    tls = importlib.import_module("modules.tls")
    mocks = _render(tmp_path, monkeypatch)
    expected = f"{CA_BUNDLE_DIR}/{tls.CA_BUNDLE_FILENAME}"
    for typ in (DEPLOYMENT, JOB):
        for _, name, inputs in [(t, n, i) for t, n, i in mocks.resources if t == typ]:
            spec = inputs["spec"]["template"]["spec"]
            volumes = {
                v["name"]: v["configMap"]["name"]
                for v in spec["volumes"]
                if "configMap" in v
            }
            for container in spec["containers"]:
                env = {e["name"]: e.get("value") for e in container["env"]}
                assert env["SSL_CERT_FILE"] == expected, name
                assert env["REQUESTS_CA_BUNDLE"] == expected, name
                (mount,) = [
                    m
                    for m in container["volumeMounts"]
                    if m["mountPath"] == CA_BUNDLE_DIR
                ]
                assert volumes[mount["name"]] == tls.CA_BUNDLE_CONFIGMAP, name


# Keys from outside the three local layers: PORT from the component,
# OTEL_RESOURCE_ATTRIBUTES from the definition's label merge. Grow this only with
# a name you found in src/ (grep it); anything else here is a deployed value
# leaking.
COMPONENT_ENV = {"PORT", "OTEL_RESOURCE_ATTRIBUTES"}


def test_env_is_exactly_the_three_local_layers(tmp_path, monkeypatch):
    """LOCAL_ENV_DEFAULTS, the definition's shared env, the stack file's env_vars."""
    mocks = _render(
        tmp_path, monkeypatch, config={"learn_ai:env_vars": {"NEW_KEY": "1"}}
    )
    env = _env(_one(mocks, DEPLOYMENT, "learn-ai-app"))
    assert set(env) == (
        set(mocks.program["LOCAL_ENV_DEFAULTS"])
        | set(definition.LOCAL_AND_DEPLOYED_ENV)
        | {"NEW_KEY"}
        | COMPONENT_ENV
    )


def test_an_override_wins_and_only_its_name_is_logged(tmp_path, monkeypatch, capsys):
    mocks = _render(
        tmp_path,
        monkeypatch,
        config={"learn_ai:env_vars": {"OPENAI_API_KEY": KEY_VALUE}},
    )
    env = _env(_one(mocks, DEPLOYMENT, "learn-ai-app"))
    assert env["OPENAI_API_KEY"] == KEY_VALUE
    err = capsys.readouterr().err
    assert "learn_ai:env_vars overrides: OPENAI_API_KEY" in err
    assert KEY_VALUE not in err


def test_the_root_domain_flows_into_hosts_and_env(tmp_path, monkeypatch):
    mocks = _render(tmp_path, monkeypatch, root_domain="mit.localhost")
    bindings = mocks.program["bindings"]
    assert bindings.hostnames.api == "ai.learn.mit.localhost"
    assert bindings.hostnames.frontend == "ai-sandbox.learn.mit.localhost"
    assert bindings.hostnames.learn_api == "api.learn.mit.localhost"
    assert bindings.cors_frontend_origins == [
        "https://learn.mit.localhost",
        "https://lms.mit.localhost",
    ]
    env = _env(_one(mocks, DEPLOYMENT, "learn-ai-app"))
    assert env["MITOL_APP_BASE_URL"] == "https://ai.learn.mit.localhost"
    # Every host in LOCAL_ENV_DEFAULTS is an f-string on the root domain; a
    # literal would pass the check above and point at the wrong cluster.
    secret = _secret_data(mocks)
    assert not [v for v in [*env.values(), *secret.values()] if "mit.test" in v]


def test_the_secret_holds_the_values_and_not_the_canvas_key(tmp_path, monkeypatch):
    mocks = _render(tmp_path, monkeypatch)
    data = _secret_data(mocks)
    assert set(data) == {"SECRET_KEY", "DATABASE_URL", "OPENAI_API_KEY", "MAILGUN_KEY"}
    webapp = _one(mocks, DEPLOYMENT, "learn-ai-app")
    env_from = webapp["spec"]["template"]["spec"]["containers"][0]["envFrom"]
    assert {"secretRef": {"name": "learn-ai-secrets"}} in env_from


def test_a_non_string_secret_value_names_its_key(tmp_path, monkeypatch):
    secrets = (APP_DIR / "secrets.yaml").read_text()
    edited = secrets.replace('OPENAI_API_KEY: ""', "OPENAI_API_KEY: 5")
    assert edited != secrets
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        _render(tmp_path, monkeypatch, app_files={"secrets.yaml": edited})


def test_a_secrets_edit_changes_the_hash_on_the_pods_and_the_job(tmp_path, monkeypatch):
    """An edit rolls the pods and re-runs the Job.

    The Job's hash has to sit on its pod template: pulumi-kubernetes replaces a
    Job only for a spec change and patches its own metadata in place.
    """

    def hashes(mocks):
        pod = _one(mocks, DEPLOYMENT, "learn-ai-app")["spec"]["template"]
        job = _one(mocks, JOB, "learn-ai-app-pre-deploy")
        rendered = _await_result(mocks, JOB, job).props
        assert "ol.mit.edu/config-hash" not in rendered["metadata"].get(
            "annotations", {}
        )
        return (
            pod["metadata"]["annotations"]["ol.mit.edu/config-hash"],
            rendered["spec"]["template"]["metadata"]["annotations"][
                "ol.mit.edu/config-hash"
            ],
        )

    before = hashes(_render(tmp_path / "a", monkeypatch))
    secrets = (APP_DIR / "secrets.yaml").read_text()
    edited = secrets.replace(
        "local-dev-learn-ai-insecure-secret-key", "another-insecure-secret-key"
    )
    assert edited != secrets
    after = hashes(
        _render(tmp_path / "b", monkeypatch, app_files={"secrets.yaml": edited})
    )
    # The pod annotation is the component's digest of every config_hash_inputs
    # entry, not the secrets.yaml digest itself, so the two values differ; both
    # must move.
    assert before[0] != after[0]
    assert before[1] != after[1]


def test_a_leftover_app_env_local_yaml_is_named_as_unread(
    tmp_path, monkeypatch, capsys
):
    """The YAML apps' override file is inert here; the apply log must say so,
    naming the keys to move (names only: the values may be secrets).
    """
    _render(
        tmp_path,
        monkeypatch,
        app_files={"configmaps/app-env.local.yaml": 'data:\n  X: "1"\n  Y: "s3cret"\n'},
    )
    err = capsys.readouterr().err
    assert "configmaps/app-env.local.yaml is not read: move X, Y under" in err
    assert "learn_ai:env_vars" in err
    assert "s3cret" not in err


def _webapp_container(mocks):
    return _one(mocks, DEPLOYMENT, "learn-ai-app")["spec"]["template"]["spec"][
        "containers"
    ][0]


def test_the_image_tilt_built_runs_and_the_published_one_otherwise(
    tmp_path, monkeypatch
):
    """Neither gets --reload: the local-dev stage Tilt builds sets GRANIAN_RELOAD
    itself, and the published image runs without reload.
    """
    built = "k3d-registry.localhost:5000/mitodl_learn-ai-app:tilt-feedface"
    container = _webapp_container(
        _render(tmp_path / "built", monkeypatch, environ={"TILT_IMAGE_0": built})
    )
    assert container["image"] == built
    assert "--reload" not in container["args"]
    container = _webapp_container(_render(tmp_path / "published", monkeypatch))
    assert container["image"] == "mitodl/learn-ai-app:latest"
    assert "--reload" not in container["args"]


def test_every_rendered_object_carries_the_deployed_labels_and_managed_by(
    tmp_path, monkeypatch
):
    """pulumi-app-apply.sh selects what it prints, and what down waits on, by
    managed-by; the rest are the deployed label set, whose keys join each
    Deployment's immutable selector.
    """
    mocks = _render(tmp_path, monkeypatch)
    rendered = [
        (t, i) for t, _, i in mocks.resources if t.startswith("kubernetes:") and i
    ]
    # Three Deployments, the Service, the Job, the Secret.
    assert len(rendered) >= 6
    for typ, inputs in rendered:
        labels = inputs["metadata"]["labels"]
        assert labels["ol.mit.edu/managed-by"] == "local-dev-pulumi", typ
        assert labels["ol.mit.edu/ou"] == "mit-learn", typ
        assert labels["ol.mit.edu/service"] == "mit-learn", typ


def _any_bindings():
    """Bindings only to read application_config's non-environment fields."""
    return definition.LearnAiBindings(
        env_suffix="dev",
        hostnames=definition.LearnAiHostnames(api="a", frontend="f", learn_api="l"),
        environment_variables={},
        k8s_labels={},
        secret_names=[],
        oidc_secret_name="x",
        application_docker_tag="latest",
        cluster=definition.ClusterCapabilities(
            registry="direct", autoscalers=False, pod_monitors=False
        ),
        cors_frontend_origins=[],
        min_replicas=1,
    )


def test_the_overrides_replace_fields_the_definition_leaves_empty():
    """model_copy replaces these wholesale.

    A value the definition set for every environment would vanish here.
    """
    config = definition.application_config(_any_bindings())
    assert config.pre_deploy_commands is None
    assert config.pre_deploy_resource_requests is None
    assert config.pre_deploy_resource_limits is None
    assert config.config_hash_inputs == {}


def _routes(mocks):
    return (
        _one(mocks, ROUTE, "learn-ai-dev-https-olapisixroute"),
        _one(mocks, ROUTE, "mit-learn-learn-ai-dev-https-olapisixroute"),
    )


def test_routes_read_the_shared_secret_with_deployed_tls_verification(
    tmp_path, monkeypatch
):
    direct, prefixed = _routes(_render(tmp_path, monkeypatch))
    for route in (direct, prefixed):
        for rule in ("passauth", "reqauth"):
            plugin = _plugin(route, rule, "openid-connect")
            assert plugin["secretRef"] == OIDC_SECRET, rule
            assert plugin["config"].get("ssl_verify", True) is True, rule


def test_route_hosts_and_paths_follow_the_root_domain(tmp_path, monkeypatch):
    mocks = _render(tmp_path, monkeypatch, root_domain="mit.localhost")
    direct, prefixed = _routes(mocks)
    for route, host in (
        (direct, "ai.learn.mit.localhost"),
        (prefixed, "api.learn.mit.localhost"),
    ):
        for rule in route["spec"]["http"]:
            assert rule["match"]["hosts"] == [host], rule["name"]
    for rule in prefixed["spec"]["http"]:
        assert all(path.startswith("/ai/") for path in rule["match"]["paths"]), rule[
            "name"
        ]
    # Hosts, cookie domains and origins all derive from the root domain.
    rendered = json.dumps(
        [i for t, _, i in mocks.resources if t in {ROUTE, PLUGIN_CONFIG}],
        default=str,
    )
    assert "mit.test" not in rendered


def test_shared_plugins_omit_opentelemetry(tmp_path, monkeypatch):
    mocks = _render(tmp_path, monkeypatch)
    config = _one(mocks, PLUGIN_CONFIG, "learn-ai-ol-shared-plugins")
    names = {p["name"] for p in config["spec"]["plugins"]}
    assert "opentelemetry" not in names
    assert {"redirect", "cors", "prometheus"} <= names


def test_the_gateway_allows_the_three_hosts_plus_learn_and_the_lms(
    tmp_path, monkeypatch
):
    mocks = _render(tmp_path, monkeypatch)
    config = _one(mocks, PLUGIN_CONFIG, "learn-ai-ol-shared-plugins")
    (cors,) = [p for p in config["spec"]["plugins"] if p["name"] == "cors"]
    assert cors["config"]["allow_origins"] == (
        "https://ai-sandbox.learn.mit.test,https://ai.learn.mit.test,"
        "https://api.learn.mit.test,https://learn.mit.test,https://lms.mit.test"
    )


def test_the_direct_and_prefixed_sessions_differ_as_deployed(tmp_path, monkeypatch):
    direct, prefixed = _routes(_render(tmp_path, monkeypatch))
    session = _plugin(direct, "passauth", "openid-connect")["config"]["session"]
    assert session["cookie_name"] == "learn_ai_apisix_session_dev"
    assert "cookie_domain" not in session
    session = _plugin(prefixed, "passauth", "openid-connect")["config"]["session"]
    assert session["cookie_name"] == "mitlearn_apisix_session_dev"
    assert session["cookie_domain"] == ".learn.mit.test"


def test_the_canvas_consumer_carries_the_secrets_yaml_key(tmp_path, monkeypatch):
    mocks = _render(tmp_path, monkeypatch)
    (consumer,) = [i for t, _, i in mocks.resources if t == CONSUMER]
    assert consumer["metadata"]["name"] == "canvas-agent"
    # The spec holds a secret Output, so the mock sees Pulumi's wire form.
    spec = consumer["spec"]
    if pulumi.runtime.rpc._special_sig_key in spec:
        spec = spec["value"]
    key = spec["authParameter"]["keyAuth"]["value"]["key"]
    assert key == "local-dev-canvas-syllabus-token"
