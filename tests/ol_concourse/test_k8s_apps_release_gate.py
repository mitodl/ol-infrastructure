"""Guard the Production job's check that the gate issue names what it deploys.

The release gate is a trigger: any closed issue matching ``Release <app>``
fires the Production job, which then deploys whatever last passed QA. These
tests pin the step that refuses a deploy the closed issue did not approve --
both where it sits in the generated plan and what the shipped script decides.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import (
    _ASSERT_GATE_SCRIPT,
    build_app_pipeline,
)

APP = "ol-analytics-api"
ASSERT_TASK = "assert-gate-names-deploy"


def _plan(app_name: str, job_suffix: str) -> list[dict[str, Any]]:
    pipeline = json.loads(build_app_pipeline(app_name).model_dump_json())
    (job,) = [j for j in pipeline["jobs"] if j["name"].endswith(job_suffix)]
    return job["plan"]


def _step_index(plan: list[dict[str, Any]], key: str, name: str) -> int:
    return next(i for i, step in enumerate(plan) if step.get(key) == name)


def test_assertion_runs_after_the_gate_and_before_anything_deploys():
    """The check sits between the gate get and the first step that deploys."""
    plan = _plan(APP, "-production")

    gate = _step_index(plan, "get", f"{APP}-release-gate")
    check = _step_index(plan, "task", ASSERT_TASK)
    deployment_start = _step_index(plan, "put", f"{APP}-deployment-production")
    pulumi_up = _step_index(plan, "put", f"pulumi-ol-application-{APP}")

    assert gate < check < deployment_start < pulumi_up


def test_assertion_reads_the_version_the_job_deploys():
    """The check compares against DOCKER_TAG's source, bound to what QA saw."""
    plan = _plan(APP, "-production")
    task = plan[_step_index(plan, "task", ASSERT_TASK)]
    release_get = plan[_step_index(plan, "get", f"{APP}-release")]

    assert task["config"]["params"]["VERSION_FILE"] == f"{APP}-release/version"
    assert release_get["passed"] == [f"deploy-ol-application-{APP}-qa"]


def test_legacy_pipelines_are_untouched():
    """Legacy pipelines have no release gate, so they get no check."""
    pipeline = json.loads(build_app_pipeline("mitxonline").model_dump_json())
    assert ASSERT_TASK not in json.dumps(pipeline)


def _run(
    tmp_path: Path, gate: dict[str, str], version: str
) -> subprocess.CompletedProcess[str]:
    gate_file = tmp_path / "gh_issue.json"
    version_file = tmp_path / "version"
    gate_file.write_text(json.dumps(gate))
    version_file.write_text(version)
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", _ASSERT_GATE_SCRIPT],
        env={
            "APP_NAME": APP,
            "GATE_FILE": str(gate_file),
            "VERSION_FILE": str(version_file),
        },
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("title", "version", "allowed"),
    [
        # The release issue for the version being deployed.
        (f"Release {APP} 2026.9.22.1", "2026.9.22.1\n", True),
        # A stale issue for an older release, closed after a newer one was cut.
        (f"Release {APP} 2026.9.17.1", "2026.9.22.1\n", False),
        # An infrastructure-only gate at the live version.
        (f"Release {APP} infrastructure @ 2026.9.17.1", "2026.9.17.1", True),
        # An infrastructure-only gate while a newer release is in the job.
        (f"Release {APP} infrastructure @ 2026.9.17.1", "2026.9.22.1", False),
        # Bare prefix, no version: approves nothing in particular.
        (f"Release {APP}", "2026.9.22.1", False),
        # Another app sharing the prefix must not match.
        (f"Release {APP}-v2 2026.9.22.1", "2026.9.22.1", False),
    ],
)
def test_script_allows_only_the_approved_version(tmp_path, title, version, allowed):
    """Only a title naming the deployed version lets the job proceed."""
    result = _run(tmp_path, {"issue_title": title}, version)
    assert (result.returncode == 0) is allowed, result.stderr


def test_script_fails_closed_on_an_empty_gate_file(tmp_path):
    """download_version writes `{}` for an empty version; that must not pass."""
    result = _run(tmp_path, {}, "2026.9.22.1")
    assert result.returncode != 0
    assert "Nothing was deployed" in result.stderr
