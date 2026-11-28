"""Abandoning a release must also neutralise its gate issue.

``release_gate`` skips issues labelled ``ops:abandoned``, but only a
step that applies the label makes that do anything. These tests pin that the
abandon job applies it, in an order that cannot retire an issue for a release
the abandon refused to cancel.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import (
    build_app_pipeline,
)

APP = "ol-analytics-api"
# A k8s app and a tag-only app: the two pipelines share the abandon job.
APPS = [APP, "ocw-hugo-themes"]


def _pipeline(app_name: str) -> dict[str, Any]:
    return json.loads(build_app_pipeline(app_name).model_dump_json())


def _abandon_plan(app_name: str = APP) -> list[dict[str, Any]]:
    (job,) = [
        j
        for j in _pipeline(app_name)["jobs"]
        if j["name"] == f"abandon-{app_name}-release"
    ]
    return job["plan"]


def _index(plan: list[dict[str, Any]], key: str, name: str) -> int:
    index = next((i for i, step in enumerate(plan) if step.get(key) == name), None)
    if index is None:
        steps = [next(iter(step.items())) for step in plan]
        msg = f"no {key!r} step named {name!r} in plan; steps are {steps}"
        raise AssertionError(msg)
    return index


def test_issue_is_retired_only_after_the_release_is_abandoned():
    """A refused abandon must leave the issue of a live release alone."""
    plan = _abandon_plan()

    abandon = _index(plan, "put", f"{APP}-release")
    retire = _index(plan, "put", f"{APP}-release-issue")

    assert abandon < retire


def test_issue_put_labels_the_version_being_abandoned():
    """The put names the abandoned version's issue and closes it, labelled."""
    plan = _abandon_plan()
    put = plan[_index(plan, "put", f"{APP}-release-issue")]
    load = plan[_index(plan, "load_var", "abandoned_version")]

    assert put["params"] == {
        "title_template": f"Release {APP} ((.:abandoned_version))",
        "close_with_labels": ["ops:abandoned"],
    }
    # The same file the abandon put reads its version from, so the two cannot
    # disagree about which release this build cancels.
    abandon = plan[_index(plan, "put", f"{APP}-release")]
    assert load["file"] == abandon["params"]["version_file"]
    # A closed issue must not be handed to the implicit get, which would
    # label it consumed.
    assert put["no_get"] is True


def test_label_matches_the_one_the_gate_skips():
    """The label applied here is the one release_gate ignores."""
    pipeline = _pipeline(APP)
    (gate,) = [r for r in pipeline["resources"] if r["name"] == f"{APP}-release-gate"]
    plan = _abandon_plan()
    put = plan[_index(plan, "put", f"{APP}-release-issue")]

    assert set(put["params"]["close_with_labels"]) <= set(
        gate["source"]["skip_if_labeled"]
    )


@pytest.mark.parametrize("app_name", APPS)
def test_abandon_cancels_the_release_in_flight_not_the_newest_version(app_name):
    """The newest version can be the next release; in_flight names the real one.

    Live on ol-analytics-api 2026-11-23: the build's own check made the next
    version newest, the put abandoned that, deleted nothing and succeeded.
    """
    plan = _abandon_plan(app_name)
    abandon = plan[_index(plan, "put", f"{app_name}-release")]
    load = plan[_index(plan, "load_var", "abandoned_version")]

    assert abandon["params"]["version_file"] == f"{app_name}-release/in_flight"
    assert load["file"] == f"{app_name}-release/in_flight"


@pytest.mark.parametrize("app_name", APPS)
def test_abandon_needs_a_request_naming_the_release(app_name):
    """The build re-reads in_flight; only a request ties it to what was seen.

    Without it, a supersede cut between `/doof abandon` and the build's check
    is the release the build cancels.
    """
    plan = _abandon_plan(app_name)
    abandon = plan[_index(plan, "put", f"{app_name}-release")]

    assert abandon["params"]["require_request"] is True


@pytest.mark.parametrize("app_name", APPS)
def test_nothing_in_flight_fails_before_anything_is_changed(app_name):
    plan = _abandon_plan(app_name)
    guard = _index(plan, "task", "assert-release-in-flight")

    assert guard < _index(plan, "load_var", "abandoned_version")
    assert guard < _index(plan, "put", f"{app_name}-release")
    assert guard < _index(plan, "put", f"{app_name}-release-issue")


def _run_guard(tmp_path: Path, in_flight: str) -> subprocess.CompletedProcess[str]:
    plan = _abandon_plan()
    config = plan[_index(plan, "task", "assert-release-in-flight")]["config"]
    in_flight_file = tmp_path / config["params"]["IN_FLIGHT_FILE"]
    in_flight_file.parent.mkdir(parents=True)
    in_flight_file.write_text(in_flight)
    return subprocess.run(  # noqa: S603
        [config["run"]["path"], *config["run"]["args"]],
        cwd=tmp_path,
        env={
            "IN_FLIGHT_FILE": config["params"]["IN_FLIGHT_FILE"],
            "PATH": "/bin:/usr/bin",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_guard_fails_when_nothing_is_in_flight(tmp_path):
    result = _run_guard(tmp_path, "")

    assert result.returncode != 0
    assert "nothing to abandon" in result.stderr


def test_guard_passes_a_release_in_flight(tmp_path):
    result = _run_guard(tmp_path, "2026.11.23.2")

    assert result.returncode == 0
    assert "Abandoning 2026.11.23.2" in result.stdout


def test_load_var_runs_before_the_abandon_deletes_anything():
    """The version is read before the abandon can change what the get saw."""
    plan = _abandon_plan()
    assert _index(plan, "load_var", "abandoned_version") < _index(
        plan, "put", f"{APP}-release"
    )


@pytest.mark.parametrize("app_name", ["mit-learn"])
def test_legacy_pipelines_have_no_abandon_job(app_name):
    """Only release-resource apps get the job, and so the issue step."""
    jobs = {j["name"] for j in _pipeline(app_name)["jobs"]}
    assert f"abandon-{app_name}-release" not in jobs
