"""Guard what triggers an API client's npm publish.

npm refuses to publish a version it already has (HTTP 403). The client repo's
VERSION only moves when ``generate-clients`` bumps it, so a publish job that
also triggers on the Node image republishes that same version every time the
image digest moves, and fails. On 2026-11-05 a ``node:24-slim`` digest bump
re-triggered the mitxonline and mit-learn publish jobs on an unchanged client
repo, and both failed that way.

A variant with ``prerelease_branch`` also publishes ``publish-next-*``
prereleases. Those mint a fresh version per build, so the same rule applies for
a different reason: a new image digest is not a new client.
"""

import json
from typing import Any

import pytest

from ol_concourse.pipelines.libraries.api_clients_pipeline import (
    generate_api_client_pipeline,
)
from ol_concourse.pipelines.libraries.configuration import PIPELINE_CONFIGS

GENERATE_JOB = "generate-clients"
PRERELEASE_VARIANTS = [
    variant
    for variant, config in PIPELINE_CONFIGS.items()
    if config.get("prerelease_branch")
]


def _pipeline(variant: str) -> dict[str, Any]:
    pipeline = generate_api_client_pipeline(**PIPELINE_CONFIGS[variant])
    return json.loads(pipeline.model_dump_json(exclude_none=True))


def _publish_jobs(variant: str) -> list[dict[str, Any]]:
    """Return the stable publish jobs, which follow generate-clients."""
    jobs = [
        job
        for job in _pipeline(variant)["jobs"]
        if job["name"].startswith("publish-")
        and not job["name"].startswith("publish-next-")
    ]
    assert jobs, f"{variant} has no publish job"
    return jobs


def _prerelease_jobs(variant: str) -> list[dict[str, Any]]:
    return [
        job
        for job in _pipeline(variant)["jobs"]
        if job["name"].startswith("publish-next-")
    ]


def _prerelease_source(variant: str) -> str:
    config = PIPELINE_CONFIGS[variant]
    return f"{config['source_repo_name']}-{config['prerelease_branch']}"


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


def test_mit_learn_publishes_prereleases_off_main():
    assert PRERELEASE_VARIANTS == ["mit-learn"]
    assert PIPELINE_CONFIGS["mit-learn"]["prerelease_branch"] == "main"


@pytest.mark.parametrize(
    "variant", [v for v in PIPELINE_CONFIGS if v not in PRERELEASE_VARIANTS]
)
def test_no_prerelease_jobs_without_a_prerelease_branch(variant):
    assert [job["name"] for job in _pipeline(variant)["jobs"]] == [
        GENERATE_JOB,
        *(job["name"] for job in _publish_jobs(variant)),
    ]


@pytest.mark.parametrize("variant", PRERELEASE_VARIANTS)
def test_prerelease_source_watches_spec_changes_on_its_branch(variant):
    config = PIPELINE_CONFIGS[variant]
    (resource,) = [
        r
        for r in _pipeline(variant)["resources"]
        if r["name"] == _prerelease_source(variant)
    ]
    assert resource["source"]["uri"] == config["source_repo_uri"]
    assert resource["source"]["branch"] == config["prerelease_branch"]
    assert resource["source"]["paths"] == ["openapi/specs/*.yaml"]


@pytest.mark.parametrize("variant", PRERELEASE_VARIANTS)
def test_a_prerelease_triggers_only_on_a_spec_change(variant):
    config = PIPELINE_CONFIGS[variant]
    subpaths = config["client_repo_subpath"]
    jobs = _prerelease_jobs(variant)
    assert [job["name"] for job in jobs] == [
        f"publish-next-{subpath}"
        for subpath in ([subpaths] if isinstance(subpaths, str) else subpaths)
    ]
    for job in jobs:
        triggers = [s["get"] for s in job["plan"] if s.get("trigger")]
        assert triggers == [_prerelease_source(variant)]
        assert "passed" not in _get(job, config["client_repo_name"])
        # Newest spec wins `next` only if builds publish in trigger order.
        assert job["serial"] is True


@pytest.mark.parametrize("variant", PRERELEASE_VARIANTS)
def test_a_prerelease_generates_from_the_branch_under_the_source_name(variant):
    """The generator configs read ``<source_repo_name>/openapi/specs/...``."""
    config = PIPELINE_CONFIGS[variant]
    for job in _prerelease_jobs(variant):
        (generate,) = [s for s in job["plan"] if s.get("task") == "generate-apis"]
        assert {
            "name": _prerelease_source(variant),
            "path": config["source_repo_name"],
        } in generate["config"]["inputs"]


@pytest.mark.parametrize("variant", PRERELEASE_VARIANTS)
def test_a_prerelease_commits_nothing_and_publishes_under_next(variant):
    for job in _prerelease_jobs(variant):
        assert not [s for s in job["plan"] if "put" in s]
        (publish,) = [s for s in job["plan"] if s.get("task") == job["name"]]
        script = publish["config"]["run"]["args"][-1]
        assert "yarn npm publish --access public --tag next" in script
        # A prerelease version, never a plain one that could pass for stable.
        assert 'npm pkg set "version=$version"' in script
        # `yarn version` spawns git, which node:*-slim lacks.
        assert "yarn version" not in script
        assert '$(date -u +%Y.%-m%d).0-dev.$(date -u +%s).g${SOURCE_SHA:0:7}"' in script
        assert publish["config"]["params"]["SOURCE_SHA"] == (
            f"((.:{_prerelease_source(variant)}-git-rev))"
        )
