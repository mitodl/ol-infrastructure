"""Guard the cross-environment promotion trigger in simple_pulumi pipelines.

A pipeline with ``prior_stage_stack`` triggers on the prior stage's
``[bot] Pulumi <project> <stack> deployed.`` issue being closed. #5669 turned
``record_deployments`` off for preview-gated pipelines, so starrocks-substructure-qa
stopped posting that issue and the Production starrocks-substructure pipeline never
triggered.
"""

import json
from typing import Any

import pytest

from ol_concourse.pipelines.infrastructure.simple_pulumi.pipeline import (
    _validate_prior_stage_producers,
    build_simple_pulumi_pipeline,
    pipeline_params,
)


def _issue_titles(app_name: str, issue_state: str) -> set[str]:
    pipeline: dict[str, Any] = json.loads(
        build_simple_pulumi_pipeline(app_name).model_dump_json(exclude_none=True)
    )
    return {
        resource["source"]["issue_title_template"]
        for resource in pipeline["resources"]
        if resource["type"] == "github-issues"
        and resource["source"]["issue_state"] == issue_state
    }


def test_starrocks_qa_posts_the_issue_production_waits_on() -> None:
    awaited = _issue_titles("starrocks-substructure", "closed")
    posted = _issue_titles("starrocks-substructure-qa", "open")
    title = "[bot] Pulumi ol-substructure-starrocks lakehouse.QA deployed."
    assert title in awaited
    assert title in posted


def test_prior_stage_without_record_is_rejected() -> None:
    params = dict(pipeline_params)
    params["starrocks-substructure-qa"] = params[
        "starrocks-substructure-qa"
    ].model_copy(update={"record_deployments": False})
    with pytest.raises(ValueError, match=r"lakehouse\.QA"):
        _validate_prior_stage_producers(params)
