"""The local mitxonline program's inputs: image, vars merge, await transform."""

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import yaml
from pulumi import ResourceOptions, ResourceTransformArgs

REPO = Path(__file__).parents[2]
MODULE = REPO / "local-dev/apps/mitxonline/pulumi/inputs.py"


def _load():
    spec = importlib.util.spec_from_file_location("mitxonline_inputs", MODULE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inputs = _load()


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        # Tilt built it: the k3d registry reference, tag included.
        (
            {
                "TILT_IMAGE_0": (
                    "k3d-registry.localhost:5000/mitodl_mitxonline-app"
                    ":tilt-e290229f85eb1e92"
                )
            },
            (
                "k3d-registry.localhost:5000/mitodl_mitxonline-app",
                "tilt-e290229f85eb1e92",
            ),
        ),
        # No checkout: the published image at the prebuilt_tags release.
        (
            {"LOCAL_DEV_PREBUILT_IMAGE_TAGS": '{"mitodl/mitxonline-app": "1.144.5"}'},
            ("mitodl/mitxonline-app", "1.144.5"),
        ),
        # No checkout and no pin: latest.
        ({}, ("mitodl/mitxonline-app", "latest")),
    ],
)
def test_image_reference(environ, expected):
    assert inputs.image_reference(environ) == expected


DEFAULTS = {"MITX_ONLINE_BASE_URL": "https://mitxonline.mit.test", "FEATURE_X": "False"}


def test_merged_env_without_vars_is_the_defaults():
    assert inputs.merged_env(DEFAULTS, None, "mit.test") == (DEFAULTS, [])


def test_merged_env_lets_vars_win_and_names_their_keys():
    env, override_keys = inputs.merged_env(
        DEFAULTS, {"FEATURE_X": "True", "NEW_KEY": "1"}, "mit.test"
    )
    assert env["FEATURE_X"] == "True"
    assert env["NEW_KEY"] == "1"
    assert override_keys == ["FEATURE_X", "NEW_KEY"]


def test_merged_env_rewrites_the_root_domain_in_vars():
    env, _ = inputs.merged_env(
        DEFAULTS, {"MITX_ONLINE_BASE_URL": "https://override.mit.test"}, "mit.localhost"
    )
    assert env["MITX_ONLINE_BASE_URL"] == "https://override.mit.localhost"


def test_merged_env_treats_a_bare_key_as_empty_like_tiltlib():
    env, _ = inputs.merged_env(DEFAULTS, {"FEATURE_X": None}, "mit.test")
    assert env["FEATURE_X"] == ""


def test_merged_env_rejects_non_string_values():
    with pytest.raises(ValueError, match="FEATURE_X"):
        inputs.merged_env(DEFAULTS, {"FEATURE_X": True}, "mit.test")


def _transform(type_: str, metadata: dict[str, Any]) -> ResourceTransformArgs:
    return ResourceTransformArgs(
        custom=True,
        type_=type_,
        name="x",
        props={"metadata": metadata},
        opts=ResourceOptions(),
    )


def test_await_policy_skips_deployments_and_keeps_other_annotations():
    result = inputs.await_policy(
        _transform(
            "kubernetes:apps/v1:Deployment",
            {"annotations": {"keep": "me"}},
        )
    )
    assert result.props["metadata"]["annotations"] == {
        "keep": "me",
        "pulumi.com/skipAwait": "true",
    }


def test_await_policy_makes_the_pre_deploy_job_fail_fast_and_stamps_it():
    args = _transform("kubernetes:batch/v1:Job", {})
    args.props["spec"] = {"template": {}}

    result = inputs.await_policy(args, job_annotations={"ol.mit.edu/config-hash": "h"})

    # The hash goes on the pod template, where a change replaces the Job and runs
    # it again; on the Job's own metadata it would only be patched in place.
    assert result.props["spec"] == {
        "template": {"metadata": {"annotations": {"ol.mit.edu/config-hash": "h"}}},
        "backoffLimit": inputs.JOB_BACKOFF_LIMIT,
    }
    assert result.props["metadata"] == {
        "annotations": {"pulumi.com/replaceUnready": "true"},
    }


def test_await_policy_leaves_other_kinds_alone():
    assert inputs.await_policy(_transform("kubernetes:core/v1:Secret", {})) is None


def test_with_local_steps_refuses_a_definition_without_pre_deploy_commands():
    """The local steps run after migrate in its shell; with no command to append
    to they would be skipped silently, so the program must stop instead.
    """
    with pytest.raises(ValueError, match="pre_deploy_commands"):
        inputs.with_local_steps(None, enabled_apps=())


@pytest.mark.parametrize("enabled", [True, False])
def test_with_local_steps_registers_the_analytics_client_only_while_enabled(enabled):
    apps = ("mitxonline", inputs.ANALYTICS_API_APP) if enabled else ("mitxonline",)

    ((_, command),) = inputs.with_local_steps(
        [("migrate", ["/bin/sh", "-c", "python manage.py migrate"])],
        enabled_apps=apps,
    )

    assert command[-1].startswith("python manage.py migrate")
    assert command[-1].endswith(inputs.ANALYTICS_API_OAUTH_APP_STEP) is enabled


def test_the_analytics_client_step_is_valid_python():
    script = inputs.ANALYTICS_API_OAUTH_APP_STEP.split("<<'PY'\n", 1)[1]
    compile(script.removesuffix("PY"), "ANALYTICS_API_OAUTH_APP_STEP", "exec")


def test_the_analytics_app_presents_the_credentials_the_job_registers():
    """secrets.yaml is the analytics app's copy of the pair; a drift between the
    two fails every org-manager check with a 502.
    """
    secret = yaml.safe_load(
        (REPO / "local-dev/apps/ol-analytics-api/secrets.yaml").read_text()
    )

    assert secret["stringData"][
        "OL_ANALYTICS_API_B2B_DASHBOARD_MITXONLINE_CLIENT_ID"
    ] == (inputs.ANALYTICS_API_CLIENT_ID)
    assert secret["stringData"][
        "OL_ANALYTICS_API_B2B_DASHBOARD_MITXONLINE_CLIENT_SECRET"
    ] == (inputs.ANALYTICS_API_CLIENT_SECRET)
