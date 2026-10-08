"""Guard the QA job's Production preview and the release issue it composes.

The QA job of a release-workflow app previews the Production stack and folds
that diff, plus the app checklist, into the one release issue the gate waits
on. These tests pin where those steps sit in the generated plan, that the
Production job is unchanged apart from its serial group, and what the script
that makes the decision does.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import (
    _CLASSIFY_SCRIPT,
    CACHE_BUST_TASK_NAME,
    CLASSIFY_TASK_NAME,
    build_app_pipeline,
)
from ol_concourse.pipelines.infrastructure.k8s_apps.scripts import classify_promotion

APP = "ol-analytics-api"
PULUMI = f"pulumi-ol-application-{APP}"
DOCKER_TAG_ENV = "OL_ANALYTICS_API_DOCKER_TAG"
RELEASE_APPS = ("ol-analytics-api", "learn-ai", "micromasters")
VERSION = "2026.10.2.1"
LIVE = "2026.10.1.1"


def _job(app_name: str, job_suffix: str) -> dict[str, Any]:
    pipeline = json.loads(build_app_pipeline(app_name).model_dump_json())
    (job,) = [j for j in pipeline["jobs"] if j["name"].endswith(job_suffix)]
    return job


def _index(plan: list[dict[str, Any]], key: str, name: str, nth: int = 0) -> int:
    matches = [i for i, step in enumerate(plan) if step.get(key) == name]
    if len(matches) <= nth:
        steps = [next(iter(step.items())) for step in plan]
        msg = f"no {key!r} step #{nth} named {name!r}; steps are {steps}"
        raise AssertionError(msg)
    return matches[nth]


def test_qa_previews_production_then_posts_the_issue():
    """Preview, then a fresh read of Production, then classify, issue, RC finish."""
    plan = _job(APP, "-qa")["plan"]

    qa_up = _index(plan, "put", PULUMI, 0)
    preview = _index(plan, "put", PULUMI, 1)
    bust = _index(plan, "task", CACHE_BUST_TASK_NAME)
    current = _index(plan, "get", f"{APP}-release-current")
    classify = _index(plan, "task", CLASSIFY_TASK_NAME)
    issue = _index(plan, "put", f"{APP}-release-issue")
    finish = _index(plan, "put", f"{APP}-deployment-rc", 1)

    assert qa_up < preview < bust < current < classify < issue < finish


def test_qa_preview_is_a_production_preview_at_the_cut_version():
    """It previews what Production would deploy: the latest cut version."""
    plan = _job(APP, "-qa")["plan"]
    preview = plan[_index(plan, "put", PULUMI, 1)]

    assert preview["params"]["stack_name"] == "Production"
    assert preview["params"]["preview"] is True
    assert preview["params"]["env_os"][DOCKER_TAG_ENV] == "((.:image_tag))"
    assert preview["get_params"]["summary_file"] == "preview_summary.md"
    assert preview["get_params"]["preview_stack"] == "Production"
    assert preview["get_params"]["read_outputs"] is False
    assert preview["inputs"] == "all"
    # Fail-closed: nothing may swallow a failed preview.
    assert not any("try" in step for step in plan)


def test_qa_still_deploys_the_cut_version_to_qa():
    """The Production preview does not change what the QA deploy itself does."""
    plan = _job(APP, "-qa")["plan"]
    qa_up = plan[_index(plan, "put", PULUMI, 0)]

    assert qa_up["params"]["stack_name"] == "QA"
    assert qa_up["params"]["env_os"][DOCKER_TAG_ENV] == "((.:image_tag))"


def test_production_state_is_read_with_a_cache_busting_get():
    """Concourse caches a get by version and params, so the params must vary."""
    plan = _job(APP, "-qa")["plan"]
    current = plan[_index(plan, "get", f"{APP}-release-current")]
    bust_var = plan[_index(plan, "load_var", "cache_bust")]

    assert current["resource"] == f"{APP}-release"
    assert current["params"] == {"cache_bust": "((.:cache_bust))"}
    assert bust_var["file"] == "cache-bust/value"
    assert _index(plan, "load_var", "cache_bust") < _index(
        plan, "get", f"{APP}-release-current"
    )


def test_classify_reads_the_fresh_production_state_and_the_preview():
    """The classify task is wired to the fresh Production state and the preview."""
    plan = _job(APP, "-qa")["plan"]
    task = plan[_index(plan, "task", CLASSIFY_TASK_NAME)]
    params = task["config"]["params"]

    assert params["VERSION_FILE"] == f"{APP}-release/version"
    assert params["PRODUCTION_VERSION_FILE"] == (
        f"{APP}-release-current/production_version"
    )
    assert params["PRODUCTION_STATE_FILE"] == f"{APP}-release-current/production_state"
    assert params["PREVIEW_SUMMARY_FILE"] == f"{PULUMI}/preview_summary.md"
    assert params["NO_CHANGES_MARKER_FILE"] == f"{PULUMI}/preview_summary.md.no-changes"


def test_issue_put_carries_the_composed_body_and_skip_marker():
    """The issue put posts the composed body, or nothing when there is none."""
    plan = _job(APP, "-qa")["plan"]
    issue = plan[_index(plan, "put", f"{APP}-release-issue")]

    assert issue["params"] == {
        "body_file": "promotion/body.md",
        "labels": ["release"],
        "title_template": "((.:promotion_title))",
        "skip_if_file": "promotion/skip",
    }
    # An issue that does not exist must not auto-close: closing approves it.
    assert "close_if_file" not in issue["params"]


def test_production_deploys_the_cut_version_the_gate_names():
    """The gate assertion binds the version, so Production needs no resolution."""
    plan = _job(APP, "-production")["plan"]
    up = plan[_index(plan, "put", PULUMI)]

    assert up["params"]["env_os"][DOCKER_TAG_ENV] == "((.:image_tag))"
    assert not any(step.get("task") == CLASSIFY_TASK_NAME for step in plan)
    assert not any(step.get("task") == CACHE_BUST_TASK_NAME for step in plan)


def test_qa_and_production_share_the_production_stack_serial_group():
    """A Production preview takes the stack lock a real deploy needs."""
    qa = _job(APP, "-qa")
    production = _job(APP, "-production")

    shared = f"ol-application-{APP}-production"
    assert shared in qa["serial_groups"]
    assert production["serial_groups"] == [shared]


@pytest.mark.parametrize("app_name", RELEASE_APPS)
def test_every_release_workflow_app_gets_the_preview(app_name):
    """Each app on the release-resource workflow gets the new steps."""
    plan = _job(app_name, "-qa")["plan"]
    assert _index(plan, "task", CLASSIFY_TASK_NAME) > 0


def test_legacy_pipelines_are_untouched():
    """Legacy pipelines have no release issue, so they get none of it."""
    pipeline = json.dumps(
        json.loads(build_app_pipeline("mitxonline").model_dump_json())
    )
    assert CLASSIFY_TASK_NAME not in pipeline
    assert CACHE_BUST_TASK_NAME not in pipeline
    assert "serial_groups" not in pipeline


@pytest.mark.parametrize(
    ("version", "production", "state", "expected"),
    [
        # Cut and not yet live: the release waiting for approval.
        (VERSION, LIVE, "known", True),
        # Already live: nothing new to approve.
        (LIVE, LIVE, "known", False),
        # Never deployed: the first release.
        (VERSION, "", "none", True),
    ],
)
def test_is_new_release(version, production, state, expected):
    """A release is new unless Production already runs this exact version."""
    assert classify_promotion.is_new_release(version, production, state) is expected


@pytest.mark.parametrize(
    ("version", "production", "state"),
    [
        (VERSION, "", "unknown"),
        (VERSION, "", ""),
        (VERSION, "", "garbage"),
        # `known` with no version is a contradiction, not a free pass.
        (VERSION, "", "known"),
        ("", LIVE, "known"),
    ],
)
def test_is_new_release_refuses_to_guess(version, production, state):
    """An undeterminable Production state raises instead of guessing."""
    with pytest.raises(ValueError, match=r"could not tell|no version"):
        classify_promotion.is_new_release(version, production, state)


@pytest.mark.parametrize(
    ("new_release", "has_diff", "title", "skip"),
    [
        (True, True, f"Release {APP} {VERSION}", False),
        (True, False, f"Release {APP} {VERSION}", False),
        (False, True, f"Release {APP} infrastructure @ {VERSION}", False),
        (False, False, "", True),
    ],
)
def test_classify_covers_the_four_rows(new_release, has_diff, title, skip):
    """The four rows of the promotion table map to a title and a skip flag."""
    assert classify_promotion.classify(
        APP, VERSION, new_release=new_release, has_diff=has_diff
    ) == (title, skip)


def _classify(  # noqa: PLR0913
    tmp_path: Path,
    *,
    production: str,
    state: str,
    summary: str | None,
    no_changes: bool,
    version: str = VERSION,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    (tmp_path / "version").write_text(version + "\n")
    (tmp_path / "production_version").write_text(production)
    (tmp_path / "production_state").write_text(state)
    (tmp_path / "checklist.md").write_text(f"## Release {version}\n\n- [ ] a by b\n")
    if summary is not None:
        (tmp_path / "preview_summary.md").write_text(summary)
    if no_changes:
        (tmp_path / "preview_summary.md.no-changes").write_text("")
    out = tmp_path / "promotion"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _CLASSIFY_SCRIPT],
        env={
            **os.environ,
            "APP_NAME": APP,
            "VERSION_FILE": str(tmp_path / "version"),
            "PRODUCTION_VERSION_FILE": str(tmp_path / "production_version"),
            "PRODUCTION_STATE_FILE": str(tmp_path / "production_state"),
            "CHECKLIST_FILE": str(tmp_path / "checklist.md"),
            "PREVIEW_SUMMARY_FILE": str(tmp_path / "preview_summary.md"),
            "NO_CHANGES_MARKER_FILE": str(tmp_path / "preview_summary.md.no-changes"),
            "OUTPUT_DIR": str(out),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, out


def test_classify_release_with_a_diff_leads_with_the_checklist(tmp_path):
    """The checklist leads so its version header stays at the top."""
    result, out = _classify(
        tmp_path,
        production=LIVE,
        state="known",
        summary="+ 1 to update",
        no_changes=False,
    )

    assert result.returncode == 0, result.stderr
    body = (out / "body.md").read_text()
    assert body.startswith(f"## Release {VERSION}")
    assert "+ 1 to update" in body
    assert not (out / "skip").exists()


def test_classify_release_without_a_diff_says_so(tmp_path):
    """A release with an empty diff still posts, stating there is no diff."""
    result, out = _classify(
        tmp_path,
        production=LIVE,
        state="known",
        summary="No changes.",
        no_changes=True,
    )

    assert result.returncode == 0, result.stderr
    assert "No infrastructure changes" in (out / "body.md").read_text()
    assert (out / "title").read_text() == f"Release {APP} {VERSION}"


def test_classify_first_release_is_a_release(tmp_path):
    """Production never deployed: the release is new even with no live version."""
    result, out = _classify(
        tmp_path, production="", state="none", summary="+ 3 to create", no_changes=False
    )

    assert result.returncode == 0, result.stderr
    assert (out / "title").read_text() == f"Release {APP} {VERSION}"


def test_classify_infrastructure_only_has_no_checklist(tmp_path):
    """An infrastructure-only issue shows the diff, not a stale checklist."""
    result, out = _classify(
        tmp_path,
        production=VERSION,
        state="known",
        summary="+ 1 to update",
        no_changes=False,
    )

    assert result.returncode == 0, result.stderr
    body = (out / "body.md").read_text()
    assert "## Release" not in body
    assert "+ 1 to update" in body
    assert (out / "title").read_text() == f"Release {APP} infrastructure @ {VERSION}"


def test_classify_nothing_to_approve_writes_the_skip_marker(tmp_path):
    """Row four posts nothing: the skip marker is written, no body is."""
    result, out = _classify(
        tmp_path,
        production=VERSION,
        state="known",
        summary="No changes.",
        no_changes=True,
    )

    assert result.returncode == 0, result.stderr
    assert (out / "skip").exists()
    assert not (out / "body.md").exists()
    # load_var still needs a title file to read.
    assert (out / "title").exists()


def test_classify_fails_closed_when_production_is_unknown(tmp_path):
    """An unreadable Production must not become a release issue or a skip."""
    result, out = _classify(
        tmp_path, production="", state="unknown", summary="x", no_changes=False
    )

    assert result.returncode != 0
    assert "could not tell" in result.stderr
    assert not (out / "title").exists()


def test_classify_fails_closed_without_a_preview_summary(tmp_path):
    """A preview that left no summary must not be read as no changes."""
    result, out = _classify(
        tmp_path, production=LIVE, state="known", summary=None, no_changes=False
    )

    assert result.returncode != 0
    assert "left no summary" in result.stderr
    assert not (out / "title").exists()
