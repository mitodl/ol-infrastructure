"""Guard the release resource's check interval on release-workflow apps.

Rebuilding the release resource image gives each pipeline's release resource a
fresh, empty version history on its next check or put. QA and Production take
the resource with ``passed:`` the release build, so until something checks the
fresh history (whose check re-emits the newest cuts) they cannot run. Under the
library's ``check_every: never`` nothing would check it until the next release.
"""

import json
from typing import Any

import pytest

from bridge.settings.apps import APPS, release_resource_workflow
from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import build_app_pipeline

RELEASE_APPS = [app for app in APPS if release_resource_workflow(app)]


def _resource(app: str) -> dict[str, Any]:
    pipeline = json.loads(build_app_pipeline(app).model_dump_json())
    (resource,) = [r for r in pipeline["resources"] if r["name"] == f"{app}-release"]
    return resource


def _gets_of(node: Any, resource: str) -> list[dict[str, Any]]:
    if isinstance(node, dict):
        found = [node] if (node.get("resource") or node.get("get")) == resource else []
        return found + [g for v in node.values() for g in _gets_of(v, resource)]
    if isinstance(node, list):
        return [g for v in node for g in _gets_of(v, resource)]
    return []


@pytest.mark.parametrize("app", RELEASE_APPS)
def test_release_resource_is_checked(app):
    assert _resource(app)["check_every"] not in (None, "never")


@pytest.mark.parametrize("app", RELEASE_APPS)
def test_a_check_cannot_trigger_anything_by_itself(app):
    """A check's candidate is not a release build's output, so it must not trigger."""
    pipeline = json.loads(build_app_pipeline(app).model_dump_json())
    for job in pipeline["jobs"]:
        for get in _gets_of(job["plan"], f"{app}-release"):
            if get.get("trigger"):
                assert get.get("passed") == [f"build-{app}-release-image"], job["name"]
