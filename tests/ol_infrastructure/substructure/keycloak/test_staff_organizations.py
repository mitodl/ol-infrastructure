"""Staff-realm organizations keep the names and settings they are deployed with.

A renamed resource is a delete and a create of an organization whose members
were added by hand, and a changed input is applied to the live organization.
"""

import asyncio

import pulumi

# Python 3.14+ compatibility: ensure event loop exists for set_mocks()
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ol_infrastructure.substructure.keycloak.org_flows import (
    create_staff_organizations,
)

REALM = "ol-platform-engineering"


class _RecordingMocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.resources: list[pulumi.runtime.MockResourceArgs] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources.append(args)
        return [f"{args.name}_id", dict(args.inputs)]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


def _create() -> dict[str, pulumi.runtime.MockResourceArgs]:
    recording = _RecordingMocks()
    pulumi.runtime.set_mocks(recording, preview=False)

    # pulumi.runtime.test waits for every resource registration to finish
    # before returning, so the mocks hold all of them once this returns.
    @pulumi.runtime.test
    def register():
        create_staff_organizations(
            REALM,
            {"Arbisoft": "arbisoft.com", "MIT": "mit.edu"},
            pulumi.ResourceOptions(),
        )

    register()
    return {args.name: args for args in recording.resources}


def test_resource_names_match_the_deployed_organizations():
    """A renamed resource deletes the organization and creates an empty one."""
    assert set(_create()) == {
        f"{REALM}-arbisoft-organization",
        f"{REALM}-mit-organization",
    }


def test_no_organization_carries_an_import_id():
    """An import id that differs from the one in state replaces the organization."""
    assert [args.resource_id for args in _create().values() if args.resource_id] == []


def test_inputs_match_the_deployed_organizations():
    """An input that differs from the live organization is applied as an update."""
    inputs = _create()[f"{REALM}-arbisoft-organization"].inputs
    assert inputs == {
        "realm": REALM,
        "name": "Arbisoft",
        "alias": "Arbisoft",
        "enabled": True,
        "domains": [{"name": "arbisoft.com", "verified": False}],
    }
