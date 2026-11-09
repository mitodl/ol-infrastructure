"""Guard what triggers an API client's npm publish.

npm refuses to publish a version it already has (HTTP 403). The client repo's
VERSION only moves when ``generate-clients`` bumps it, so a publish job that
also triggers on the Node image republishes that same version every time the
image digest moves, and fails. On 2026-11-05 a ``node:24-slim`` digest bump
re-triggered the mitxonline and mit-learn publish jobs on an unchanged client
repo, and both failed that way.
"""

import json
from typing import Any

import pytest

from ol_concourse.pipelines.libraries.api_clients_pipeline import (
    generate_api_client_pipeline,
)
from ol_concourse.pipelines.libraries.configuration import PIPELINE_CONFIGS

GENERATE_JOB = "generate-clients"


def _pipeline(variant: str) -> dict[str, Any]:
    pipeline = generate_api_client_pipeline(**PIPELINE_CONFIGS[variant])
    return json.loads(pipeline.model_dump_json(exclude_none=True))


def _publish_jobs(variant: str) -> list[dict[str, Any]]:
    jobs = [
        job for job in _pipeline(variant)["jobs"] if job["name"].startswith("publish-")
    ]
    assert jobs, f"{variant} has no publish job"
    return jobs


def _get(job: dict[str, Any], resource: str) -> dict[str, Any]:
    (step,) = [s for s in job["plan"] if s.get("get") == resource]
    return step


@pytest.mark.parametrize("variant", PIPELINE_CONFIGS)
def test_a_new_node_image_does_not_trigger_a_publish(variant):
    for job in _publish_jobs(variant):
        assert not _get(job, "node-image").get("trigger"), (
            f"{variant}/{job['name']} republishes the same version on every "
            "Node image digest bump, and npm refuses that"
        )


@pytest.mark.parametrize("variant", PIPELINE_CONFIGS)
def test_a_publish_still_triggers_on_a_generated_client(variant):
    """The one trigger a publish needs: generate-clients pushed a new version."""
    client_repo = PIPELINE_CONFIGS[variant]["client_repo_name"]
    for job in _publish_jobs(variant):
        step = _get(job, client_repo)
        assert step["trigger"] is True
        assert step["passed"] == [GENERATE_JOB]
