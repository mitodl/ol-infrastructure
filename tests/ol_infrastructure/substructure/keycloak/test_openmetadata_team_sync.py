"""The team-sync client calls the Admin API, so its token needs the audience."""

import asyncio

import pulumi
import pulumi_keycloak as keycloak
import pytest

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ol_infrastructure.substructure.keycloak.client_policies import (
    ADMIN_API_SCOPE_NAME,
)
from ol_infrastructure.substructure.keycloak.openmetadata_team_sync import (
    create_openmetadata_team_sync_resources,
)


class _RecordingMocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.resources: list[pulumi.runtime.MockResourceArgs] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources.append(args)
        outputs = dict(args.inputs)
        if args.typ == "keycloak:openid/client:Client":
            outputs["clientSecret"] = "test-secret"  # pragma: allowlist secret
        return [f"{args.name}_id", outputs]

    def call(self, args: pulumi.runtime.MockCallArgs):
        if args.token == "keycloak:openid/getClient:getClient":  # noqa: S105
            return {"id": "realm-management-id"}
        return {}


@pytest.fixture
def mocks():
    recording = _RecordingMocks()
    pulumi.runtime.set_mocks(recording, preview=False)

    @pulumi.runtime.test
    def register():
        realm = keycloak.Realm("ol-data-platform", realm="ol-data-platform")
        create_openmetadata_team_sync_resources(
            keycloak_provider=None,
            keycloak_url="https://keycloak.example",
            realm=realm,
        )

    register()
    return recording


def test_team_sync_client_gets_admin_api_scope_by_default(mocks):
    """A service-account-only client takes the Admin API audience as a default."""
    (defaults,) = [
        r
        for r in mocks.resources
        if r.name == "ol-openmetadata-team-sync-default-scopes"
    ]
    assert ADMIN_API_SCOPE_NAME in defaults.inputs["defaultScopes"]
    assert {"basic", "roles"} <= set(defaults.inputs["defaultScopes"])
