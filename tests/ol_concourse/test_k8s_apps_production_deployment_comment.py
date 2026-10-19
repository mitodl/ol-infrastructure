"""The Production job comments its Deployment on the release issue it deploys.

Closing the release issue authorizes the promotion; it does not mean the release
is live. The finish put on the Production Deployment is handed the gate issue's
``gh_issue.json`` so the github-deployments resource can post a link to it.
"""

import json
from typing import Any

from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import (
    build_app_pipeline,
)

APP = "ol-analytics-api"


def _plan(job_suffix: str) -> list[dict[str, Any]]:
    pipeline = json.loads(build_app_pipeline(APP).model_dump_json())
    (job,) = [j for j in pipeline["jobs"] if j["name"].endswith(job_suffix)]
    return job["plan"]


def _walk(steps: list[dict[str, Any]]):
    """Yield every step, including those nested in do/try/in_parallel blocks."""
    for step in steps:
        yield step
        for key in ("do", "in_parallel"):
            nested = step.get(key)
            if isinstance(nested, dict):
                nested = nested.get("steps")
            if nested:
                yield from _walk(nested)
        if isinstance(step.get("try"), dict):
            yield from _walk([step["try"]])


def _finish_puts(plan: list[dict[str, Any]], resource: str) -> list[dict[str, Any]]:
    return [
        step
        for step in _walk(plan)
        if step.get("put") == resource and step["params"].get("action") == "finish"
    ]


def test_the_production_finish_names_the_gate_issue_file():
    """The resource is told which issue to comment on, from the gate's get."""
    plan = _plan("-production")

    (finish,) = _finish_puts(plan, f"{APP}-deployment-production")

    assert finish["params"]["comment_on_issue_file"] == (
        f"{APP}-release-gate/gh_issue.json"
    )
    assert finish["params"]["state"] == "success"


def test_the_gate_the_comment_reads_is_fetched_before_the_finish():
    """The file named above exists in the build only if the gate is fetched first."""
    plan = list(_walk(_plan("-production")))

    gate = next(
        i for i, step in enumerate(plan) if step.get("get") == f"{APP}-release-gate"
    )
    finish = next(
        i
        for i, step in enumerate(plan)
        if step.get("put") == f"{APP}-deployment-production"
        and step["params"].get("action") == "finish"
    )

    assert gate < finish


def test_the_rc_finish_does_not_comment():
    """The release issue is still open at RC; it is the Production gate."""
    plan = _plan("-qa")

    (finish,) = _finish_puts(plan, f"{APP}-deployment-rc")

    assert "comment_on_issue_file" not in finish["params"]
