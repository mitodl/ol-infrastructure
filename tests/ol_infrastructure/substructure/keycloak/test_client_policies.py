"""The PKCE client policy must match marked clients only and never rewrite one.

Keycloak validates every Admin API write of a client the policy matches, so a
condition that matches more than the marked clients fails the next deploy of the
others.
"""

import asyncio
import json

import pulumi
import pytest

# Python 3.14+ compatibility: ensure event loop exists for set_mocks()
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ol_infrastructure.substructure.keycloak.client_policies import (
    PKCE_REQUIRED_CLIENT_ARGS,
    create_pkce_client_policy,
)

PREFIX = "ol-data-platform"


class _RecordingMocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.resources: list[pulumi.runtime.MockResourceArgs] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources.append(args)
        return [f"{args.name}_id", dict(args.inputs)]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


@pytest.fixture
def resources() -> dict[str, pulumi.runtime.MockResourceArgs]:
    """Register the policy and return its resources by Pulumi name."""
    recording = _RecordingMocks()
    pulumi.runtime.set_mocks(recording, preview=False)

    @pulumi.runtime.test
    def register():
        create_pkce_client_policy("ol-data-platform", PREFIX)

    register()
    return {args.name: args for args in recording.resources}


def test_profile_enforces_pkce_without_rewriting_the_client(resources):
    """With auto-configure on, Keycloak edits the client instead of rejecting it."""
    profile = resources[f"{PREFIX}-pkce-client-profile"].inputs
    assert profile["executors"] == [
        {"name": "pkce-enforcer", "configuration": {"auto-configure": "false"}}
    ]


def test_policy_matches_the_marker_attribute_only(resources):
    """Keycloak reads the attributes value as a JSON-encoded string."""
    policy = resources[f"{PREFIX}-pkce-client-policy"].inputs
    assert policy["enabled"] is True
    assert policy["profiles"] == [
        resources[f"{PREFIX}-pkce-client-profile"].inputs["name"]
    ]
    (condition,) = policy["conditions"]
    assert condition["name"] == "client-attributes"
    attributes = condition["configuration"]["attributes"]
    assert isinstance(attributes, str)
    assert {
        entry["key"]: entry["value"] for entry in json.loads(attributes)
    } == PKCE_REQUIRED_CLIENT_ARGS["extra_config"]


def test_a_marked_client_always_carries_s256():
    """The policy rejects the next update of a marked client without S256."""
    assert PKCE_REQUIRED_CLIENT_ARGS["pkce_code_challenge_method"] == "S256"
