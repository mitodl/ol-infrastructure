"""The client policies must match marked clients only and never rewrite one.

Keycloak validates every Admin API write of a client a policy matches, so a
condition that matches more than the marked clients fails the next deploy of the
others.
"""

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

import pulumi
import pytest

# Python 3.14+ compatibility: ensure event loop exists for set_mocks()
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ol_infrastructure.substructure.keycloak import client_policies
from ol_infrastructure.substructure.keycloak.client_policies import (
    MANAGED_CLIENT_ARGS,
    MANAGED_MARKER_ATTRIBUTE,
    MANAGED_MARKER_VALUE,
    PKCE_MARKER_ATTRIBUTE,
    PKCE_MARKER_VALUE,
    PKCE_REQUIRED_CLIENT_ARGS,
    create_client_policies,
)
from tests.ol_infrastructure.recording_mocks import RecordingMocks

PREFIX = "ol-data-platform"


@pytest.fixture
def resources() -> dict[str, pulumi.runtime.MockResourceArgs]:
    """Register the policies and return their resources by Pulumi name."""
    recording = RecordingMocks()
    pulumi.runtime.set_mocks(recording, preview=False)

    @pulumi.runtime.test
    def register():
        create_client_policies("ol-data-platform", PREFIX)

    register()
    return {args.name: args for args in recording.resources}


def _condition_attributes(policy_inputs: dict[str, Any]) -> dict[str, str]:
    (condition,) = policy_inputs["conditions"]
    assert condition["name"] == "client-attributes"
    attributes = condition["configuration"]["attributes"]
    # Keycloak reads the attributes value as a JSON-encoded string.
    assert isinstance(attributes, str)
    return {entry["key"]: entry["value"] for entry in json.loads(attributes)}


def test_pkce_profile_enforces_pkce_without_rewriting_the_client(resources):
    """With auto-configure on, Keycloak edits the client instead of rejecting it."""
    profile = resources[f"{PREFIX}-pkce-client-profile"].inputs
    assert profile["executors"] == [
        {"name": "pkce-enforcer", "configuration": {"auto-configure": "false"}}
    ]


def test_pkce_policy_matches_the_pkce_marker_only(resources):
    """A condition on access type would also match the built-in clients."""
    policy = resources[f"{PREFIX}-pkce-client-policy"].inputs
    assert policy["enabled"] is True
    assert policy["profiles"] == [
        resources[f"{PREFIX}-pkce-client-profile"].inputs["name"]
    ]
    assert _condition_attributes(policy) == {PKCE_MARKER_ATTRIBUTE: PKCE_MARKER_VALUE}


def test_grants_profile_rejects_both_grants_without_rewriting_the_client(resources):
    """Same auto-configure rule as the PKCE profile."""
    profile = resources[f"{PREFIX}-grants-client-profile"].inputs
    assert profile["executors"] == [
        {"name": "reject-implicit-grant", "configuration": {"auto-configure": "false"}},
        {"name": "reject-ropc-grant", "configuration": {"auto-configure": "false"}},
    ]


def test_managed_policy_matches_the_managed_marker_only(resources):
    """An any-client condition would also match admin-cli and the SAML clients."""
    policy = resources[f"{PREFIX}-managed-client-policy"].inputs
    assert policy["enabled"] is True
    assert policy["profiles"] == [
        resources[f"{PREFIX}-grants-client-profile"].inputs["name"]
    ]
    assert _condition_attributes(policy) == {
        MANAGED_MARKER_ATTRIBUTE: MANAGED_MARKER_VALUE
    }


@pytest.mark.parametrize(
    "client_args", [MANAGED_CLIENT_ARGS, PKCE_REQUIRED_CLIENT_ARGS]
)
def test_a_managed_client_always_has_both_grants_off(client_args):
    """The managed policy rejects the next update of a marked client with either on."""
    assert client_args["extra_config"][MANAGED_MARKER_ATTRIBUTE] == MANAGED_MARKER_VALUE
    assert client_args["implicit_flow_enabled"] is False
    assert client_args["direct_access_grants_enabled"] is False


KEYCLOAK_PACKAGE = Path(client_policies.__file__).parent
# The master realm has no client policies, so its clients carry no marker.
UNMARKED_CLIENT_MODULES = {"scim.py"}
CLIENT_ARG_SPREADS = {"MANAGED_CLIENT_ARGS", "PKCE_REQUIRED_CLIENT_ARGS"}


def _client_calls_without_spread() -> list[str]:
    missing = []
    for module in sorted(KEYCLOAK_PACKAGE.glob("*.py")):
        if module.name in UNMARKED_CLIENT_MODULES:
            continue
        for node in ast.walk(ast.parse(module.read_text())):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "Client"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "openid"
            ):
                continue
            spreads = {
                keyword.value.id
                for keyword in node.keywords
                if keyword.arg is None and isinstance(keyword.value, ast.Name)
            }
            if not spreads & CLIENT_ARG_SPREADS:
                missing.append(f"{module.name}:{node.lineno}")
    return missing


def test_every_realm_client_carries_a_marker():
    """A client declared without a spread escapes both policies silently."""
    assert _client_calls_without_spread() == []


def test_a_pkce_client_always_carries_s256():
    """The PKCE policy rejects the next update of a marked client without S256."""
    assert PKCE_REQUIRED_CLIENT_ARGS["pkce_code_challenge_method"] == "S256"
    assert (
        PKCE_REQUIRED_CLIENT_ARGS["extra_config"][PKCE_MARKER_ATTRIBUTE]
        == PKCE_MARKER_VALUE
    )
