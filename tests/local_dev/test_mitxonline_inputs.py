"""The local mitxonline program's inputs: image, vars merge, await transform."""

import importlib.util
import os
import subprocess
import sys
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


# Stands in for the image's python: every `manage.py <command>` succeeds, and
# `manage.py shell` compiles its stdin, as Django's shell does before exec, and
# appends it to $SHELL_LOG.
_STUB_PYTHON = f"""#!{sys.executable}
import os, sys
if sys.argv[1:3] == ["manage.py", "shell"]:
    source = sys.stdin.read()
    compile(source, "manage.py shell stdin", "exec")
    with open(os.environ["SHELL_LOG"], "a") as log:
        log.write(source + "\\0")
"""


@pytest.mark.parametrize("enabled", [True, False])
def test_the_composed_pre_deploy_command_runs_each_script_in_one_shell(
    tmp_path, enabled
):
    """The steps are only checked as a whole command: a heredoc that swallows
    the next step is valid bash, and each script compiles on its own.
    """
    apps = ("mitxonline", inputs.ANALYTICS_API_APP) if enabled else ("mitxonline",)
    ((_, command),) = inputs.with_local_steps(
        [("migrate", ["/bin/bash", "-c", "python manage.py migrate --noinput"])],
        enabled_apps=apps,
    )
    stub = tmp_path / "python"
    stub.write_text(_STUB_PYTHON)
    stub.chmod(0o755)
    log = tmp_path / "shell.log"

    result = subprocess.run(  # noqa: S603
        [*command[:-1], command[-1]],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "SHELL_LOG": str(log),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    (shell_input,) = log.read_text().split("\0")[:-1]
    assert "OPENEDX_OAUTH_APP_CLIENT_ID" in shell_input
    assert (inputs.ANALYTICS_API_CLIENT_ID in shell_input) is enabled


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
