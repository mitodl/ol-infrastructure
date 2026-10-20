"""The staff-realm browser flow must keep its names and have no REQUIRED top level.

Keycloak ignores the ALTERNATIVE steps of a flow level that also has a REQUIRED
one, so a single REQUIRED top-level step turns cookie SSO off. The realm's flow
binding points at the flow alias, so a changed name replaces the bound flow.
"""

import asyncio

import pulumi
import pytest

# Python 3.14+ compatibility: ensure event loop exists for set_mocks()
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ol_infrastructure.substructure.keycloak.passkey_flow import (
    create_passkey_browser_flow,
)

PREFIX = "ol-browser-data-platform"


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
    """Register the flow and return its resources by Pulumi name."""
    recording = _RecordingMocks()
    pulumi.runtime.set_mocks(recording, preview=False)

    # pulumi.runtime.test waits for every resource registration to finish
    # before returning, so the mocks hold all of them once this returns.
    @pulumi.runtime.test
    def register():
        create_passkey_browser_flow("ol-data-platform", PREFIX)

    register()
    return {args.name: args for args in recording.resources}


def test_resource_names_and_aliases_match_the_deployed_flow(resources):
    """A renamed resource or alias replaces the flow the realm is bound to."""
    assert set(resources) == {
        f"{PREFIX}-flow",
        f"{PREFIX}-auth-cookie",
        f"{PREFIX}-idp-redirector",
        f"{PREFIX}-passkey-flow",
        f"{PREFIX}-flow-username-form",
        f"{PREFIX}-webauthn-authenticator-flow",
    }
    assert resources[f"{PREFIX}-flow"].inputs["alias"] == f"{PREFIX}-flow"
    assert (
        resources[f"{PREFIX}-passkey-flow"].inputs["alias"] == f"{PREFIX}-passkey-flow"
    )


def test_every_top_level_step_is_alternative(resources):
    """One REQUIRED top-level step makes Keycloak skip the cookie step."""
    top_level = {
        name: args.inputs["requirement"]
        for name, args in resources.items()
        if args.inputs.get("parentFlowAlias") == f"{PREFIX}-flow"
    }
    assert top_level == {
        f"{PREFIX}-auth-cookie": "ALTERNATIVE",
        f"{PREFIX}-idp-redirector": "ALTERNATIVE",
        f"{PREFIX}-passkey-flow": "ALTERNATIVE",
    }


def test_passkey_subflow_requires_username_then_passkey(resources):
    """The username form alone succeeds for anyone who types a valid username."""
    steps = sorted(
        (
            args.inputs["priority"],
            args.inputs["authenticator"],
            args.inputs["requirement"],
        )
        for args in resources.values()
        if args.inputs.get("parentFlowAlias") == f"{PREFIX}-passkey-flow"
    )
    assert steps == [
        (70, "auth-username-form", "REQUIRED"),
        (80, "webauthn-authenticator-passwordless", "REQUIRED"),
    ]
