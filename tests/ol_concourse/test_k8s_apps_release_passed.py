"""Guard which release versions Production can deploy on release-workflow apps.

Concourse records a job's inputs by resource, not by step name, and Production
takes the release ``passed:`` QA. So every version QA resolves the release
resource to, under any step name, is one Production can deploy. Two failures
on micromasters (2026-10-27) pin the shape these tests check:

- QA's second get of the release resource had no ``passed:``, so it took the
  resource's latest check: the next, uncut release once the main branch had
  moved past the last one. Production resolved to it.
- Fixing that by adding the release build to Production's ``passed:``
  (mitodl/ol-infrastructure#6684, reverted in #6685) bound Production to an
  older QA build than the newest, so its ol-infrastructure commit was not the
  one the gate issue approved.

Both were reproduced against the live Concourse scheduler with mock
resources. These tests pin the shape that resolved every case correctly
there; they cannot exercise the scheduler themselves.
"""

import json
from typing import Any

import pytest

from bridge.settings.apps import APPS, release_resource_workflow
from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import build_app_pipeline

RELEASE_APPS = [app for app in APPS if release_resource_workflow(app)]


def _job(app_name: str, name: str) -> dict[str, Any]:
    pipeline = json.loads(build_app_pipeline(app_name).model_dump_json())
    (job,) = [j for j in pipeline["jobs"] if j["name"] == name]
    return job


def _gets(node: Any) -> list[dict[str, Any]]:
    """Every get step in a plan, at any depth (hooks, try, in_parallel, do)."""
    if isinstance(node, dict):
        found = [node] if "get" in node else []
        return found + [g for v in node.values() for g in _gets(v)]
    if isinstance(node, list):
        return [g for v in node for g in _gets(v)]
    return []


def _release_gets(job: dict[str, Any], app: str) -> list[dict[str, Any]]:
    return [
        g for g in _gets(job) if (g.get("resource") or g["get"]) == f"{app}-release"
    ]


@pytest.mark.parametrize("app", RELEASE_APPS)
def test_every_qa_get_of_the_release_goes_through_the_release_build(app):
    """QA resolves the release resource only to versions the release build cut."""
    qa = _job(app, f"deploy-ol-application-{app}-qa")
    gets = _release_gets(qa, app)

    assert {g["get"] for g in gets} == {f"{app}-release", f"{app}-release-current"}
    for get in gets:
        assert get.get("passed") == [f"build-{app}-release-image"], get["get"]


@pytest.mark.parametrize("app", RELEASE_APPS)
def test_production_takes_release_and_image_through_qa_alone(app):
    """Production's release and image are ``passed:`` QA and nothing else."""
    production = _job(app, f"deploy-ol-application-{app}-production")
    by_name = {g["get"]: g for g in _gets(production)}

    for name in (f"{app}-release", f"{app}-app-release-ecr-image"):
        assert by_name[name]["passed"] == [f"deploy-ol-application-{app}-qa"], name


@pytest.mark.parametrize("app", RELEASE_APPS)
def test_no_production_get_names_two_passed_jobs(app):
    """A get ``passed:`` two jobs bound Production to an older QA build (#6685)."""
    production = _job(app, f"deploy-ol-application-{app}-production")

    for get in _gets(production):
        assert len(get.get("passed") or []) <= 1, get["get"]
