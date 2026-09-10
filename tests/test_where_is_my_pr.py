"""Regression tests for the read-only PR deployment tracing script."""

import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the extensionless script without invoking its CLI."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "where-is-my-pr"
    loader = SourceFileLoader("where_is_my_pr", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, loader.name, module)
    loader.exec_module(module)
    # No test may accidentally reach GitHub or Concourse.
    monkeypatch.setattr(
        module, "_gh_json", Mock(side_effect=AssertionError("GitHub call"))
    )
    monkeypatch.setattr(
        module, "_fly_curl", Mock(side_effect=AssertionError("fly call"))
    )
    monkeypatch.setattr(module, "_fly_ready", Mock(return_value=True))
    return module


@pytest.fixture
def pr(script: ModuleType) -> object:
    """Create a merged infrastructure PR like #5809 without secret values."""
    return script.PullRequest(
        owner="mitodl",
        repo="ol-infrastructure",
        number=5809,
        title="Replicate the Fastly purge key into QA",
        state="MERGED",
        merged=True,
        commit="a" * 40,
        merged_at="2026-09-10T14:09:13Z",
    )


@pytest.mark.parametrize("env", ["ci", "qa", "production"])
@pytest.mark.parametrize("family", ["concourse/operations", "vault/secrets"])
def test_routes_match_generated_pipeline(
    script: ModuleType,
    env: str,
    family: str,
) -> None:
    """Guard explicit mappings against actual pipeline jobs, inputs and watches."""
    from ol_concourse.pipelines.infrastructure.concourse.pipeline import (  # noqa: PLC0415
        concourse_pipeline,
    )
    from ol_concourse.pipelines.infrastructure.vault.pipeline import (  # noqa: PLC0415
        vault_pipeline,
    )

    path = f"src/bridge/secrets/{family}.{env}.yaml"
    route, matched_env = script._matching_secrets_route(path)
    assert matched_env == env
    pipeline = (
        concourse_pipeline() if family.startswith("concourse/") else vault_pipeline
    )
    jobs = {job.name: job.model_dump(exclude_none=True) for job in pipeline.jobs}
    deploy = jobs[f"deploy-{route.job_suffix}-{env}"]
    resource_name = (
        "ol-infrastructure-pulumi"
        if family.startswith("concourse/")
        else "ol-infrastructure-pulumi-substructure"
    )
    assert route.resource == resource_name
    assert resource_name in str(deploy["plan"])
    resource = next(r for r in pipeline.resources if r.name == resource_name)
    assert any(
        path.startswith(prefix)
        for prefix in resource.model_dump(mode="json")["source"]["paths"]
    )
    if env != "ci":
        assert f"preview-{route.job_suffix}-{env}" in jobs


@pytest.mark.parametrize(
    "path",
    [
        "src/bridge/secrets/pulumi/vault.qa.yaml",
        "src/bridge/secrets/concourse/operations.dev.yaml",
        "src/bridge/secrets/concourse/operations.qa.yaml.bak",
        "src/bridge/secrets/unmapped/secrets.qa.yaml",
        "src/bridge/secrets/vault/README.md",
    ],
)
def test_unmapped_files(script: ModuleType, path: str) -> None:
    """Do not treat provider credentials or unknown files as Vault deployments."""
    assert script._matching_secrets_route(path) is None


@pytest.mark.parametrize("family", ["concourse/operations", "vault/secrets"])
@pytest.mark.parametrize("reached", [True, False, None])
def test_report_checks_only_environment_deploy(  # noqa: PLR0913
    *,
    script: ModuleType,
    pr: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    family: str,
    reached: bool | None,
) -> None:
    """Successful deploy inputs, not previews or branch tips, supply the status."""
    path = f"src/bridge/secrets/{family}.qa.yaml"
    monkeypatch.setattr(script, "_changed_files", Mock(return_value=[path]))
    live = Mock(return_value=script.LiveStage(reached, "42", None, "b" * 40))
    monkeypatch.setattr(script, "_live_stage", live)
    script._report_ol_infrastructure_pr(pr)
    output = capsys.readouterr().out
    route, _ = script._matching_secrets_route(path)
    resource = (
        "ol-infrastructure-pulumi"
        if family.startswith("concourse/")
        else "ol-infrastructure-pulumi-substructure"
    )
    live.assert_called_once_with(
        pr,
        route.pipeline,
        f"deploy-{route.job_suffix}-qa",
        [resource],
    )
    assert f"Vault {route.vault_mount} (QA)" in output
    assert "manual deploy after preview" in output
    assert "preview does not write secrets" in output
    assert f"/jobs/deploy-{route.job_suffix}-qa/builds/42" in output
    assert "no known" not in output
    if reached is None:
        assert "?  Vault deploy" in output
    else:
        assert ("not yet reached" if not reached else "✓  Vault deploy") in output


@pytest.mark.parametrize("fly_ready", [True, False])
def test_missing_evidence_stays_unknown(
    *,
    script: ModuleType,
    pr: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fly_ready: bool,
) -> None:
    """Missing login/build evidence must never turn a merge into deploy success."""
    path = "src/bridge/secrets/concourse/operations.qa.yaml"
    monkeypatch.setattr(script, "_changed_files", Mock(return_value=[path]))
    monkeypatch.setattr(script, "_fly_ready", Mock(return_value=fly_ready))
    live = Mock(return_value=None)
    monkeypatch.setattr(script, "_live_stage", live)
    script._report_ol_infrastructure_pr(pr)
    output = capsys.readouterr().out
    assert "✓" not in output
    assert "?" in output
    assert "packer-pulumi-concourse/jobs/deploy-" in output
    if fly_ready:
        assert "Could not verify" in output
    else:
        live.assert_not_called()
        assert "fly -t infrastructure login" in output


def test_mixed_pr_preserves_app_and_unknown_paths(
    script: ModuleType,
    pr: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Secrets reporting must not suppress existing app or unmatched-file output."""
    monkeypatch.setattr(
        script,
        "_changed_files",
        Mock(
            return_value=[
                "src/bridge/secrets/concourse/operations.ci.yaml",
                "src/bridge/secrets/concourse/operations.production.yaml",
                "src/ol_infrastructure/applications/mitxonline/__main__.py",
                "README.md",
            ]
        ),
    )
    monkeypatch.setattr(script, "_fly_ready", Mock(return_value=False))
    app_report = Mock()
    monkeypatch.setattr(script, "_report_ol_infra_app", app_report)
    script._report_ol_infrastructure_pr(pr)
    output = capsys.readouterr().out
    app_report.assert_called_once_with(pr, "mitxonline")
    assert "Vault secrets: CI" in output
    assert "Vault secrets: Production" in output
    assert "CI deploys automatically" in output
    assert (
        "Other changed files (no known deployment pipeline mapping):\n    README.md"
        in output
    )


@pytest.mark.parametrize(
    ("status", "expected"), [("ahead", True), ("behind", False), (None, None)]
)
def test_live_stage_compares_deploy_input(
    *,
    script: ModuleType,
    pr: object,
    monkeypatch: pytest.MonkeyPatch,
    status: str | None,
    expected: bool | None,
) -> None:
    """The underlying live check compares the PR SHA with the successful input SHA."""
    curl = Mock(
        side_effect=[
            [
                {"id": 99, "name": "43", "status": "failed"},
                {"id": 98, "name": "42", "status": "succeeded"},
            ],
            {
                "inputs": [
                    {"name": "ol-infrastructure-pulumi", "version": {"ref": "b" * 40}}
                ]
            },
        ]
    )
    monkeypatch.setattr(script, "_fly_curl", curl)
    compare = Mock(return_value=status)
    monkeypatch.setattr(script, "_compare_status", compare)
    live = script._live_stage(
        pr, "packer-pulumi-concourse", "deploy-job", ["ol-infrastructure-pulumi"]
    )
    assert live.reached is expected
    assert live.build_name == "42"
    compare.assert_called_once_with("mitodl", "ol-infrastructure", "a" * 40, "b" * 40)
    assert curl.call_args.args == ("/api/v1/builds/98/resources",)
