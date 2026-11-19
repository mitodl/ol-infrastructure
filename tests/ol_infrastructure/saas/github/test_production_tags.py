"""The `production/*` tag ruleset: who may write a Production tag, and on which repos.

A Production tag authorizes a stable API-client publish, so the ruleset has to
exist on every repo whose releases write one, and let only ol-release-bot (and
org owners, for a hand-made recovery) through.
"""

import asyncio
from typing import Any

import pulumi
import pulumi_github as github
import pytest

# Python 3.14+ compatibility: ensure event loop exists for set_mocks()
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from bridge.settings.apps import (
    APPS,
    PRODUCTION_TAG_PREFIX,
    RELEASE_BOT_APP_ID,
    github_repo,
    release_resource_workflow,
)
from ol_infrastructure.saas.github.repositories import archetypes, rulesets
from tests.ol_infrastructure.recording_mocks import RecordingMocks


def _build(repo: dict[str, Any]) -> dict[str, pulumi.runtime.MockResourceArgs]:
    recording = RecordingMocks()
    pulumi.runtime.set_mocks(recording, preview=False)

    @pulumi.runtime.test
    def register():
        repository = github.Repository(f"mitodl-repo-{repo['name']}", name=repo["name"])
        rulesets.build_production_tags(repo, repository)

    register()
    return {
        args.name: args
        for args in recording.resources
        if args.typ == "github:index/repositoryRuleset:RepositoryRuleset"
    }


def _ruleset(name: str) -> dict[str, Any]:
    (args,) = _build({"name": name}).values()
    return args.inputs


@pytest.mark.parametrize("app", [app for app in APPS if release_resource_workflow(app)])
def test_every_repo_that_writes_production_tags_protects_them(app):
    name = github_repo(app).removeprefix("mitodl/")
    assert list(_build({"name": name})) == [f"mitodl-repo-production-tags-{name}"]


@pytest.mark.parametrize(
    "app", [app for app in APPS if not release_resource_workflow(app)]
)
def test_a_registered_repo_not_yet_on_the_workflow_is_protected_too(app):
    """Pre-provisioned, so flipping an app's pipeline needs no ruleset edit."""
    name = github_repo(app).removeprefix("mitodl/")
    assert list(_build({"name": name})) == [f"mitodl-repo-production-tags-{name}"]


def test_only_the_release_bot_and_org_owners_may_write_them():
    inputs = _ruleset("mitxonline")

    assert inputs["name"] == rulesets.PRODUCTION_TAGS_RULESET_NAME
    assert inputs["target"] == "tag"
    assert inputs["enforcement"] == "active"
    assert inputs["conditions"]["refName"] == {
        "includes": [f"refs/tags/{PRODUCTION_TAG_PREFIX}*"],
        "excludes": [],
    }
    assert inputs["rules"] == {"creation": True, "update": True, "deletion": True}
    assert inputs["bypassActors"] == [
        {
            "actorType": "Team",
            "actorId": archetypes.TEAM_IDS["odl-engineering-owners"],
            "bypassMode": "always",
        },
        {
            "actorType": "Integration",
            "actorId": RELEASE_BOT_APP_ID,
            "bypassMode": "always",
        },
    ]


def test_a_repo_outside_the_release_registry_gets_none():
    assert _build({"name": "ol-infrastructure"}) == {}


def test_an_archived_repo_gets_none():
    """GitHub refuses ruleset writes on an archived repo."""
    assert _build({"name": "mitxonline", "archived": True}) == {}
