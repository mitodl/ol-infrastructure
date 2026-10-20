"""Guard which release-workflow apps get a generated CHANGELOG.md entry.

The release resource inserts its entry ahead of the first ``## `` line of
``CHANGELOG.md``. In a file another tool manages (ol-analytics-api's scriv
collects fragments during the bump, and anchors each entry with a line above its
heading) that lands between the anchor and the heading and duplicates the
release, so such an app opts out with ``AppRegistration.release_changelog``.
"""

import json
from typing import Any

import pytest

from bridge.settings.apps import APPS, release_changelog, release_resource_workflow
from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import build_app_pipeline

RELEASE_APPS = [app for app in APPS if release_resource_workflow(app)]


def _release_source(app: str) -> dict[str, Any]:
    pipeline = json.loads(build_app_pipeline(app).model_dump_json())
    (resource,) = [r for r in pipeline["resources"] if r["name"] == f"{app}-release"]
    return resource["source"]


@pytest.mark.parametrize("app", RELEASE_APPS)
def test_changelog_follows_the_registry(app):
    source = _release_source(app)
    if release_changelog(app):
        assert source["changelog_style"] == "cumulative"
        assert source["changelog_file"] == "CHANGELOG.md"
    else:
        assert "changelog_style" not in source


def test_ol_analytics_api_keeps_its_own_changelog():
    assert not release_changelog("ol-analytics-api")
    assert "changelog_style" not in _release_source("ol-analytics-api")


@pytest.mark.parametrize("app", ["learn-ai", "micromasters"])
def test_apps_without_their_own_changelog_get_the_generated_one(app):
    assert _release_source(app)["changelog_style"] == "cumulative"


def test_unregistered_app_defaults_to_the_generated_changelog():
    assert release_changelog("not-a-registered-app")
