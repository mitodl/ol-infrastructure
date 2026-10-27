"""Render the local mitxonline program under mocks; pin what must match deployed."""

import asyncio
import importlib.util
import json
import runpy
import shutil
from pathlib import Path
from typing import Any

import pulumi

from ol_infrastructure.applications.mitxonline import definition

# Python 3.14+ compatibility, as
# tests/ol_infrastructure/applications/mitxonline/test_definition.py does.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

REPO = Path(__file__).parents[2]
PROGRAM_DIR = REPO / "local-dev/apps/mitxonline/pulumi"
APP_DIR = REPO / "local-dev/apps/mitxonline"
OIDC_SECRET = "ol-mitlearn-oidc"  # noqa: S105  # pragma: allowlist secret
ROUTE = "kubernetes:apisix.apache.org/v2:ApisixRoute"
PLUGIN_CONFIG = "kubernetes:apisix.apache.org/v2:ApisixPluginConfig"
DEPLOYMENT = "kubernetes:apps/v1:Deployment"
SERVICE = "kubernetes:core/v1:Service"
JOB = "kubernetes:batch/v1:Job"
APP_COMPONENT = "ol:infrastructure:components:services:OLApplicationK8s"


class RecordingMocks(pulumi.runtime.Mocks):
    def __init__(self):
        self.resources: list[tuple[str, str, dict[str, Any]]] = []
        self.transforms: dict[str, list[Any]] = {}
        self.program: dict[str, Any] = {}

    def reset(self):
        self.resources.clear()
        self.transforms.clear()

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources.append((args.typ, args.name, args.inputs))
        if args.transforms:
            self.transforms[args.typ] = list(args.transforms)
        return [f"{args.name}-id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


MOCKS = RecordingMocks()


def _tree(
    tmp_path: Path, secrets_extra: str = "", app_files: dict[str, str] | None = None
) -> Path:
    """Copy the app dir with pulumi/ inside; the program takes its parent as APP_DIR."""
    root = tmp_path / "local-dev"
    shutil.copytree(
        APP_DIR, root / "apps/mitxonline", ignore=shutil.ignore_patterns("__pycache__")
    )
    if secrets_extra:
        with (root / "apps/mitxonline/secrets.yaml").open("a") as f:
            f.write(secrets_extra)
    for relpath, text in (app_files or {}).items():
        target = root / "apps/mitxonline" / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return root / "apps/mitxonline/pulumi"


def _render(  # noqa: PLR0913
    tmp_path,
    monkeypatch,
    *,
    root_domain="mit.test",
    config=None,
    environ=None,
    secrets_extra="",
    app_files=None,
):
    for key in (
        "TILT_IMAGE_0",
        "LOCAL_DEV_PREBUILT_IMAGE_TAGS",
        "LOCAL_DEV_ANALYTICS_API_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LOCAL_DEV_ROOT_DOMAIN", root_domain)
    for key, value in (environ or {}).items():
        monkeypatch.setenv(key, value)
    MOCKS.reset()
    # Set per render, not at import: other test modules set their own mocks at
    # import time and the last one wins.
    pulumi.runtime.set_mocks(
        MOCKS,
        project="ol-local-dev-mitxonline",
        stack="Dev",
        preview=False,
    )
    # The stack file's config: block, as the CLI hands it to the program (an
    # object value arrives JSON-encoded).
    pulumi.runtime.set_all_config(
        {
            key: value if isinstance(value, str) else json.dumps(value)
            for key, value in (config or {}).items()
        }
    )
    program_dir = _tree(tmp_path, secrets_extra, app_files)

    @pulumi.runtime.test
    def run():
        MOCKS.program = runpy.run_path(str(program_dir), run_name="__main__")
        return pulumi.Output.from_input(True)  # noqa: FBT003

    run()
    return MOCKS


def _one(mocks, typ, name):
    matches = [
        i
        for t, _, i in mocks.resources
        if t == typ and i.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1, f"{typ} {name}: {len(matches)} found"
    return matches[0]


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
    webapp = _one(mocks, DEPLOYMENT, "mitxonline-app")
    containers = webapp["spec"]["template"]["spec"]["containers"]
    assert [c["name"] for c in containers] == ["mitxonline-app"]  # no nginx sidecar
    assert containers[0]["ports"][0]["containerPort"] == 8073
    service = _one(mocks, SERVICE, "mitxonline-webapp")
    assert service["spec"]["ports"][0]["port"] == 8073
    # await_policy itself is covered in test_mitxonline_inputs.py; here, that the
    # component gets it.
    (transform,) = mocks.transforms[APP_COMPONENT]
    assert getattr(transform, "func", transform).__name__ == "await_policy"


def _resources(workload):
    return workload["spec"]["template"]["spec"]["containers"][0]["resources"]


def test_workloads_are_laptop_sized(tmp_path, monkeypatch):
    mocks = _render(tmp_path, monkeypatch)
    assert _resources(_one(mocks, DEPLOYMENT, "mitxonline-app")) == {
        "requests": {"cpu": "100m", "memory": "512Mi"},
        "limits": {"memory": "1Gi"},
    }
    assert _resources(_one(mocks, JOB, "mitxonline-app-pre-deploy")) == {
        "requests": {"cpu": "100m", "memory": "512Mi"},
        "limits": {"memory": "2Gi"},
    }
    assert _resources(_one(mocks, DEPLOYMENT, "mitxonline-all-celery-worker")) == {
        "requests": {"cpu": "50m", "memory": "256Mi"},
        "limits": {"memory": "1Gi"},
    }
    assert _resources(_one(mocks, DEPLOYMENT, "mitxonline-celery-beat")) == {
        "requests": {"cpu": "10m", "memory": "384Mi"},
        "limits": {"memory": "384Mi"},
    }


def test_the_stack_file_sets_memory_limits_and_leaves_requests(tmp_path, monkeypatch):
    """The deployed stacks' web_memory_limit key and three siblings."""
    mocks = _render(
        tmp_path,
        monkeypatch,
        config={
            "mitxonline:web_memory_limit": "2Gi",
            "mitxonline:worker_memory_limit": "3Gi",
            "mitxonline:pre_deploy_memory_limit": "4Gi",
            "mitxonline:beat_memory_limit": "512Mi",
        },
    )
    limits = {
        name: _resources(_one(mocks, typ, name))["limits"]["memory"]
        for typ, name in (
            (DEPLOYMENT, "mitxonline-app"),
            (DEPLOYMENT, "mitxonline-all-celery-worker"),
            (JOB, "mitxonline-app-pre-deploy"),
            (DEPLOYMENT, "mitxonline-celery-beat"),
        )
    }
    assert limits == {
        "mitxonline-app": "2Gi",
        "mitxonline-all-celery-worker": "3Gi",
        "mitxonline-app-pre-deploy": "4Gi",
        "mitxonline-celery-beat": "512Mi",
    }
    assert _resources(_one(mocks, DEPLOYMENT, "mitxonline-app"))["requests"] == {
        "cpu": "100m",
        "memory": "512Mi",
    }


def test_every_container_mounts_each_path_once(tmp_path, monkeypatch):
    """The component appends extra_volume_mounts to extra init containers.

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
    expected = f"{mocks.program['CA_BUNDLE_DIR']}/{tls.CA_BUNDLE_FILENAME}"
    for typ in (DEPLOYMENT, JOB):
        for _, name, inputs in [(t, n, i) for t, n, i in mocks.resources if t == typ]:
            spec = inputs["spec"]["template"]["spec"]
            volumes = {
                v["name"]: v["configMap"]["name"]
                for v in spec["volumes"]
                if "configMap" in v
            }
            assert not [
                c["name"]
                for c in spec.get("initContainers") or []
                if c["name"] == "ca-bundle-merge"
            ]
            for container in spec["containers"]:
                env = {e["name"]: e.get("value") for e in container["env"]}
                assert env["SSL_CERT_FILE"] == expected, name
                assert env["REQUESTS_CA_BUNDLE"] == expected, name
                (mount,) = [
                    m
                    for m in container["volumeMounts"]
                    if m["mountPath"] == mocks.program["CA_BUNDLE_DIR"]
                ]
                assert volumes[mount["name"]] == tls.CA_BUNDLE_CONFIGMAP, name


def test_the_pre_deploy_job_is_the_deployed_command_plus_the_oauth_app_step(
    tmp_path, monkeypatch
):
    mocks = _render(tmp_path, monkeypatch)
    job = _one(mocks, JOB, "mitxonline-app-pre-deploy")
    (container,) = job["spec"]["template"]["spec"]["containers"]
    ((name, deployed),) = definition.application_config(
        _any_bindings()
    ).pre_deploy_commands
    assert container["name"] == name
    assert container["command"][:-1] == deployed[:-1]
    # Appended to the same shell, so they run after migrate, never beside it:
    # configure_wagtail for a fresh database, then the OAuth2 Application row.
    steps = _inputs().CONFIGURE_WAGTAIL_STEP + _inputs().OPENEDX_OAUTH_APP_STEP
    assert container["command"][-1] == deployed[-1] + steps
    # The step reads the pair from the Job's own env.
    assert {"OPENEDX_OAUTH_APP_CLIENT_ID", "OPENEDX_OAUTH_APP_CLIENT_SECRET"} <= {
        e["name"] for e in container["env"] if "value" in e
    }


def _inputs():
    spec = importlib.util.spec_from_file_location(
        "mitxonline_inputs", PROGRAM_DIR / "inputs.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _any_bindings():
    """Bindings only to read application_config's non-environment fields."""
    return definition.MitxonlineBindings(
        env_suffix="dev",
        hostnames=definition.MitxonlineHostnames(api="a", frontend="f", learn_api="l"),
        environment_variables={},
        k8s_labels={},
        secret_names=[],
        oidc_secret_name="x",
        application_docker_tag="latest",
        cluster=definition.ClusterCapabilities(
            registry="direct", autoscalers=False, pod_monitors=False
        ),
        min_replicas=1,
        sizing=definition.MitxonlineSizing(
            web=definition.WorkloadResources(
                requests={"memory": "1Gi"}, limits={"memory": "1Gi"}
            )
        ),
    )


def test_routes_read_the_shared_secret_with_deployed_tls_verification(
    tmp_path, monkeypatch
):
    mocks = _render(tmp_path, monkeypatch)
    direct = _one(mocks, ROUTE, "mitxonline-apisix-route-direct-dev")
    oidc = _plugin(direct, "passauth", "openid-connect")
    assert oidc["secretRef"] == OIDC_SECRET
    assert oidc["config"]["ssl_verify"] is True
    assert "discovery" not in oidc["config"]
    assert (
        oidc["config"]["post_logout_redirect_uri"]
        == "https://api.mitxonline.mit.test/logout/"
    )
    assert set(direct["spec"]["http"][0]["match"]["hosts"]) == {
        "api.mitxonline.mit.test",
        "mitxonline.mit.test",
    }
    prefixed = _one(mocks, ROUTE, "mitxonline-apisix-route-prefixed-dev")
    oidc = _plugin(prefixed, "passauth", "openid-connect")
    assert oidc["secretRef"] == OIDC_SECRET
    assert oidc["config"]["session"]["cookie_name"] == "mitlearn_apisix_session_dev"
    assert oidc["config"]["session"]["cookie_domain"] == ".learn.mit.test"
    assert prefixed["spec"]["http"][0]["match"]["hosts"] == ["api.learn.mit.test"]


def test_shared_plugins_omit_opentelemetry(tmp_path, monkeypatch):
    mocks = _render(tmp_path, monkeypatch)
    config = _one(mocks, PLUGIN_CONFIG, "mitxonline-ol-shared-plugins")
    names = {p["name"] for p in config["spec"]["plugins"]}
    assert "opentelemetry" not in names
    assert {"redirect", "cors", "prometheus", "serverless-pre-function"} <= names


def test_the_gateway_allows_the_origins_django_does(tmp_path, monkeypatch):
    mocks = _render(tmp_path, monkeypatch, root_domain="mit.localhost")
    config = _one(mocks, PLUGIN_CONFIG, "mitxonline-ol-shared-plugins")
    (cors,) = [p for p in config["spec"]["plugins"] if p["name"] == "cors"]
    origins = "https://mitxonline.mit.localhost,https://learn.mit.localhost"
    assert cors["config"]["allow_origins"] == origins
    assert _env(_one(mocks, DEPLOYMENT, "mitxonline-app"))["CORS_ALLOWED_ORIGINS"] == (
        origins
    )


# Keys from outside the three local layers: PORT from the component,
# OTEL_RESOURCE_ATTRIBUTES from the definition's label merge. Grow this only with
# a name you found in src/ (grep it); anything else here is a deployed value
# leaking.
COMPONENT_ENV = {"PORT", "OTEL_RESOURCE_ATTRIBUTES"}


def test_env_is_exactly_the_three_local_layers(tmp_path, monkeypatch):
    """LOCAL_ENV_DEFAULTS, the definition's shared env, the stack file's vars."""
    mocks = _render(tmp_path, monkeypatch, config={"mitxonline:vars": {"NEW_KEY": "1"}})
    env = _env(_one(mocks, DEPLOYMENT, "mitxonline-app"))
    assert set(env) == (
        set(mocks.program["LOCAL_ENV_DEFAULTS"])
        | set(definition.LOCAL_AND_DEPLOYED_ENV)
        | {"NEW_KEY"}
        | COMPONENT_ENV
    )


def test_the_root_domain_flows_into_hosts_and_env(tmp_path, monkeypatch):
    mocks = _render(tmp_path, monkeypatch, root_domain="mit.localhost")
    direct = _one(mocks, ROUTE, "mitxonline-apisix-route-direct-dev")
    assert set(direct["spec"]["http"][0]["match"]["hosts"]) == {
        "api.mitxonline.mit.localhost",
        "mitxonline.mit.localhost",
    }
    env = _env(_one(mocks, DEPLOYMENT, "mitxonline-app"))
    assert env["MITX_ONLINE_BASE_URL"] == "https://mitxonline.mit.localhost"
    assert (
        env["KEYCLOAK_DISCOVERY_URL"]
        == "https://sso.ol.mit.localhost/realms/olapps/.well-known/openid-configuration"
    )
    # Every host in LOCAL_ENV_DEFAULTS is an f-string on the root domain; a
    # literal would pass the two checks above and point at the wrong cluster.
    assert not [v for v in env.values() if "mit.test" in v]


def _webapp_container(mocks):
    return _one(mocks, DEPLOYMENT, "mitxonline-app")["spec"]["template"]["spec"][
        "containers"
    ][0]


def test_the_image_tilt_built_runs_with_reload_and_the_published_one_without(
    tmp_path, monkeypatch
):
    """The published image has no granian[reload]; --reload there exits at startup."""
    built = "k3d-registry.localhost:5000/mitodl_mitxonline-app:tilt-feedface"
    container = _webapp_container(
        _render(tmp_path / "built", monkeypatch, environ={"TILT_IMAGE_0": built})
    )
    assert container["image"] == built
    assert "--reload" in container["args"]
    container = _webapp_container(_render(tmp_path / "published", monkeypatch))
    assert container["image"] == "mitodl/mitxonline-app:latest"
    assert "--reload" not in container["args"]


def test_a_secrets_edit_changes_the_hash_on_the_pods_and_the_job(tmp_path, monkeypatch):
    """The README promises an edit rolls the pods and re-runs the Job.

    The Job's hash has to sit on its pod template: pulumi-kubernetes replaces a
    Job only for a spec change and patches its own metadata in place.
    """

    def hashes(mocks):
        pod = _one(mocks, DEPLOYMENT, "mitxonline-app")["spec"]["template"]
        job = _one(mocks, JOB, "mitxonline-app-pre-deploy")
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
    after = hashes(
        _render(tmp_path / "b", monkeypatch, secrets_extra='  NEW_SECRET: "1"\n')
    )
    # The component folds its own sources into the pod hash, so the two differ;
    # both must move.
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
    assert "s3cret" not in err


def test_every_rendered_object_carries_the_managed_by_label(tmp_path, monkeypatch):
    """pulumi-app-apply.sh selects what it prints and tears down by this label."""
    mocks = _render(tmp_path, monkeypatch)
    rendered = [
        (t, i) for t, _, i in mocks.resources if t.startswith("kubernetes:") and i
    ]
    assert len(rendered) >= 6  # Deployments, Service, Job, routes, plugins, Secret
    for typ, inputs in rendered:
        assert (
            inputs["metadata"]["labels"]["ol.mit.edu/managed-by"] == "local-dev-pulumi"
        ), typ


def test_the_provider_pins_the_home_kubeconfig_not_the_environments(
    tmp_path, monkeypatch
):
    """Tilt's KUBECONFIG is a file that exists only while Tilt runs."""
    mocks = _render(tmp_path, monkeypatch, environ={"KUBECONFIG": "/tilt/cluster.yml"})
    (provider,) = [
        i for t, _, i in mocks.resources if t == "pulumi:providers:kubernetes"
    ]
    assert provider["context"] == "local-dev"
    assert provider["kubeconfig"] == str(Path.home() / ".kube" / "config")


def test_the_overrides_replace_fields_the_definition_leaves_empty():
    """model_copy replaces these wholesale.

    A value the definition set for every environment would vanish here.
    """
    config = definition.application_config(_any_bindings())
    assert config.extra_init_containers == []
    assert config.extra_volumes == []
    assert config.extra_volume_mounts == []
    assert config.config_hash_inputs == {}
    assert len(config.pre_deploy_commands) == 1
