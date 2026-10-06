"""Staff-realm organizations are adopted by id, never created alongside the originals.

The organizations already exist in every environment. Declared without the
existing id, Keycloak rejects the create because the alias is taken.
"""

import asyncio

import pulumi
import pytest

# Python 3.14+ compatibility: ensure event loop exists for set_mocks()
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ol_infrastructure.substructure.keycloak.org_flows import (
    create_staff_organizations,
)

REALM = "ol-platform-engineering"
IMPORT_IDS = {
    "Arbisoft": "8668d7c8-52f4-4ea8-b123-1eeaa77362c9",
    "MIT": "5f7bcdac-2ca6-4970-be16-abab3f058a25",
}


class _RecordingMocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.resources: list[pulumi.runtime.MockResourceArgs] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources.append(args)
        return [args.resource_id or f"{args.name}_id", dict(args.inputs)]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


def _create(import_ids: dict[str, str]) -> dict[str, pulumi.runtime.MockResourceArgs]:
    recording = _RecordingMocks()
    pulumi.runtime.set_mocks(recording, preview=False)

    # pulumi.runtime.test waits for every resource registration to finish
    # before returning, so the mocks hold all of them once this returns.
    @pulumi.runtime.test
    def register():
        create_staff_organizations(
            REALM,
            {"Arbisoft": "arbisoft.com", "MIT": "mit.edu"},
            import_ids,
            pulumi.ResourceOptions(),
        )

    register()
    return {args.name: args for args in recording.resources}


def test_each_organization_is_imported_by_its_realm_scoped_id():
    """The import id is `<realm>/<organization id>`."""
    resources = _create(IMPORT_IDS)
    assert {name: args.resource_id for name, args in resources.items()} == {
        f"{REALM}-arbisoft-organization": f"{REALM}/{IMPORT_IDS['Arbisoft']}",
        f"{REALM}-mit-organization": f"{REALM}/{IMPORT_IDS['MIT']}",
    }


def test_inputs_match_the_hand_made_organizations():
    """An input that differs from the live organization is applied as an update."""
    inputs = _create(IMPORT_IDS)[f"{REALM}-arbisoft-organization"].inputs
    assert inputs == {
        "realm": REALM,
        "name": "Arbisoft",
        "alias": "Arbisoft",
        "enabled": True,
        "domains": [{"name": "arbisoft.com", "verified": False}],
    }


def test_missing_import_id_fails():
    """An environment with no id recorded must not create a second organization."""
    with pytest.raises(KeyError):
        _create({"MIT": IMPORT_IDS["MIT"]})
