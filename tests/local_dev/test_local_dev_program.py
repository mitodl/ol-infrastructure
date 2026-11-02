"""The helpers shared by the Tilt-run programs under local-dev/apps/*/pulumi."""

import importlib
from pathlib import Path
from typing import Any

import pulumi
import pytest
from pulumi import ResourceOptions, ResourceTransformArgs

from ol_infrastructure.lib.local_dev_program import (
    CA_BUNDLE_CONFIGMAP,
    CA_BUNDLE_DIR,
    CA_BUNDLE_FILE,
    JOB_BACKOFF_LIMIT,
    await_policy,
    ca_bundle_env,
    image_reference,
    k3d_provider,
    merged_env,
    secrets_hash,
    warn_leftover_app_env_local,
)
from tests.local_dev.program_render import ProgramMocks

REPO = Path(__file__).parents[2]
PROVIDER_TYPE = "pulumi:providers:kubernetes"


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
    assert image_reference("mitodl/mitxonline-app", environ) == expected


DEFAULTS = {"MITX_ONLINE_BASE_URL": "https://mitxonline.mit.test", "FEATURE_X": "False"}
SECTION = "mitxonline:vars"


def test_merged_env_without_vars_is_the_defaults():
    assert merged_env(SECTION, DEFAULTS, None, "mit.test") == (DEFAULTS, [])


def test_merged_env_lets_vars_win_and_names_their_keys():
    env, override_keys = merged_env(
        SECTION, DEFAULTS, {"FEATURE_X": "True", "NEW_KEY": "1"}, "mit.test"
    )
    assert env["FEATURE_X"] == "True"
    assert env["NEW_KEY"] == "1"
    assert override_keys == ["FEATURE_X", "NEW_KEY"]


def test_merged_env_rewrites_the_root_domain_in_vars():
    env, _ = merged_env(
        SECTION,
        DEFAULTS,
        {"MITX_ONLINE_BASE_URL": "https://override.mit.test"},
        "mit.localhost",
    )
    assert env["MITX_ONLINE_BASE_URL"] == "https://override.mit.localhost"


def test_merged_env_treats_a_bare_key_as_empty_like_tiltlib():
    env, _ = merged_env(SECTION, DEFAULTS, {"FEATURE_X": None}, "mit.test")
    assert env["FEATURE_X"] == ""


def test_merged_env_rejects_non_string_values():
    with pytest.raises(ValueError, match="FEATURE_X") as error:
        merged_env(SECTION, DEFAULTS, {"FEATURE_X": True}, "mit.test")
    assert str(error.value).startswith(SECTION)


def _transform(type_: str, metadata: dict[str, Any]) -> ResourceTransformArgs:
    return ResourceTransformArgs(
        custom=True,
        type_=type_,
        name="x",
        props={"metadata": metadata},
        opts=ResourceOptions(),
    )


def test_await_policy_skips_deployments_and_keeps_other_annotations():
    result = await_policy(
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

    result = await_policy(args, job_annotations={"ol.mit.edu/config-hash": "h"})

    # The hash goes on the pod template, where a change replaces the Job and runs
    # it again; on the Job's own metadata it would only be patched in place.
    assert result.props["spec"] == {
        "template": {"metadata": {"annotations": {"ol.mit.edu/config-hash": "h"}}},
        "backoffLimit": JOB_BACKOFF_LIMIT,
    }
    assert result.props["metadata"] == {
        "annotations": {"pulumi.com/replaceUnready": "true"},
    }


def test_await_policy_leaves_other_kinds_alone():
    assert await_policy(_transform("kubernetes:core/v1:Secret", {})) is None


def test_ca_bundle_names_match_the_core_stacks_module(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO / "local-dev/infra"))
    tls = importlib.import_module("modules.tls")
    assert CA_BUNDLE_CONFIGMAP == tls.CA_BUNDLE_CONFIGMAP
    assert f"{CA_BUNDLE_DIR}/{tls.CA_BUNDLE_FILENAME}" == CA_BUNDLE_FILE
    assert ca_bundle_env() == {
        "REQUESTS_CA_BUNDLE": CA_BUNDLE_FILE,
        "SSL_CERT_FILE": CA_BUNDLE_FILE,
    }


def test_the_leftover_warning_names_keys_not_values(tmp_path, capsys):
    (tmp_path / "configmaps").mkdir()
    (tmp_path / "configmaps/app-env.local.yaml").write_text(
        'data:\n  X: "1"\n  Y: "s3cret"\n'
    )
    warn_leftover_app_env_local(tmp_path, "learn_ai:env_vars")
    err = capsys.readouterr().err
    assert "move X, Y under learn_ai:env_vars" in err
    assert "s3cret" not in err


def test_no_leftover_file_prints_nothing(tmp_path, capsys):
    warn_leftover_app_env_local(tmp_path, "learn_ai:env_vars")
    assert capsys.readouterr().err == ""


def test_secrets_hash_is_order_independent():
    assert secrets_hash({"a": "1", "b": "2"}) == secrets_hash({"b": "2", "a": "1"})
    assert secrets_hash({"a": "1"}) != secrets_hash({"a": "2"})


def test_k3d_provider_pins_the_home_kubeconfig_not_the_environments(monkeypatch):
    """Tilt's KUBECONFIG is a file that exists only while Tilt runs."""
    monkeypatch.setenv("KUBECONFIG", "/tilt/cluster.yml")
    mocks = ProgramMocks()
    pulumi.runtime.set_mocks(mocks, project="p", stack="s", preview=False)

    @pulumi.runtime.test
    def run():
        k3d_provider()

    run()
    (provider,) = [i for t, _, i in mocks.resources if t == PROVIDER_TYPE]
    assert provider["context"] == "local-dev"
    assert provider["kubeconfig"] == str(Path.home() / ".kube" / "config")
