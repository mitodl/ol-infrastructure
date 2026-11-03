"""Guard the pinned image tags of the release flow's custom resource types.

Concourse keys a resource's version history on its type's image. While these
types tracked ``latest``, each image publish emptied every app's release
history on its next check, stranding release issues closed before the next QA
build. See RELEASE_RESOURCE_TYPE_TAGS.
"""

import json
import re
from typing import Any

import pytest

from bridge.settings.apps import APPS, release_resource_workflow
from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import (
    RELEASE_RESOURCE_TYPE_TAGS,
    build_app_pipeline,
)

RELEASE_APPS = [app for app in APPS if release_resource_workflow(app)]
LEGACY_APPS = [app for app in APPS if not release_resource_workflow(app)]

_REPOSITORIES = {
    "release": "mitodl/concourse-release-resource",
    "github-issues": "mitodl/concourse-github-issues-resource",
    "github-deployments": "mitodl/concourse-github-deployments-resource",
}


def _resource_types(app: str) -> dict[str, dict[str, Any]]:
    pipeline = json.loads(build_app_pipeline(app).model_dump_json())
    return {rt["name"]: rt for rt in pipeline["resource_types"]}


def test_every_pin_is_a_full_commit_sha():
    """ol-concourse tags each image with the full SHA it was built from."""
    assert set(RELEASE_RESOURCE_TYPE_TAGS) == set(_REPOSITORIES)
    for tag in RELEASE_RESOURCE_TYPE_TAGS.values():
        assert re.fullmatch(r"[0-9a-f]{40}", tag), tag


@pytest.mark.parametrize("app", RELEASE_APPS)
def test_release_workflow_resource_types_are_pinned(app):
    resource_types = _resource_types(app)
    for name, repository in _REPOSITORIES.items():
        source = resource_types[name]["source"]
        assert source["repository"] == repository, (app, name)
        assert source["tag"] == RELEASE_RESOURCE_TYPE_TAGS[name], (app, name)


@pytest.mark.parametrize("app", LEGACY_APPS)
def test_legacy_pipelines_are_not_changed(app):
    """Pinning a legacy pipeline's github-issues type would reset its gate too."""
    github_issues = _resource_types(app).get("github-issues")
    if github_issues is not None:
        assert github_issues["source"].get("tag", "latest") == "latest"
