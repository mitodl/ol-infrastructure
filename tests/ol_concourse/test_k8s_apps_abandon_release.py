"""Abandoning a release must also neutralise its gate issue.

``release_gate`` is configured ``skip_if_labeled=["abandoned"]``, but only a
step that applies the label makes that do anything. These tests pin that the
abandon job applies it, in an order that cannot retire an issue for a release
the abandon refused to cancel.
"""

import json
from typing import Any

import pytest

from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import (
    build_app_pipeline,
)

APP = "ol-analytics-api"


def _pipeline(app_name: str) -> dict[str, Any]:
    return json.loads(build_app_pipeline(app_name).model_dump_json())


def _abandon_plan() -> list[dict[str, Any]]:
    (job,) = [
        j for j in _pipeline(APP)["jobs"] if j["name"] == f"abandon-{APP}-release"
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
        "close_with_labels": ["abandoned"],
    }
    # The same file the abandon put reads its version from, so the two cannot
    # disagree about which release this build cancels.
    abandon = plan[_index(plan, "put", f"{APP}-release")]
    assert load["file"] == abandon["params"]["version_file"]
    # A closed issue must not be handed to the implicit get, which would
    # rename it [CONSUMED ...] and lose its version.
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


def test_load_var_runs_before_the_abandon_deletes_anything():
    """The version is read before the abandon can change what the get saw."""
    plan = _abandon_plan()
    assert _index(plan, "load_var", "abandoned_version") < _index(
        plan, "put", f"{APP}-release"
    )


@pytest.mark.parametrize("app_name", ["mitxonline", "mit-learn"])
def test_legacy_pipelines_have_no_abandon_job(app_name):
    """Only release-resource apps get the job, and so the issue step."""
    jobs = {j["name"] for j in _pipeline(app_name)["jobs"]}
    assert f"abandon-{app_name}-release" not in jobs
