"""Memory-service Keycloak resources must give out memory tokens only on request.

The memory service (applications/hindsight) accepts any token naming its
audience and takes the learner from ``sub``. If the audience scope were a
default scope, every session token would be a memory token, so these tests pin
it as optional on app clients, the realm's existing optional scopes alongside
it, the ``sub`` mapper on the scope, and the admin client's grants.
"""

import asyncio
import json

import pulumi
import pulumi_keycloak as keycloak
import pytest

# Python 3.14+ compatibility: ensure event loop exists for set_mocks()
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ol_infrastructure.substructure.keycloak.ol_memory import (
    ADMIN_CLIENT_ID,
    AUDIENCE_CLIENT_ID,
    AUDIENCE_SCOPE_NAME,
    GENERAL_SCOPE_NAME,
    LEARN_AI_AUDIENCE_SCOPE_NAME,
    REALM_DEFAULT_OPTIONAL_SCOPES,
    attach_memory_audience,
    create_ol_memory_resources,
)

LEARN_AI_CLIENT_ID = "ol-learn-ai-client"
MITLEARN_CLIENT_ID = "ol-mitlearn-client"


class _RecordingMocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.resources: list[pulumi.runtime.MockResourceArgs] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources.append(args)
        outputs = dict(args.inputs)
        if args.typ == "keycloak:openid/client:Client":
            outputs["clientSecret"] = "test-secret"  # pragma: allowlist secret
        return [f"{args.name}_id", outputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}

    def of_type(self, typ: str) -> list[pulumi.runtime.MockResourceArgs]:
        """Return the registered resources of one Pulumi type."""
        return [r for r in self.resources if r.typ == typ]

    def named(self, name: str) -> pulumi.runtime.MockResourceArgs:
        """Return the one registered resource with this Pulumi name."""
        (resource,) = [r for r in self.resources if r.name == name]
        return resource


@pytest.fixture
def mocks():
    """Record every resource the helpers register, as olapps.py calls them."""
    recording = _RecordingMocks()
    pulumi.runtime.set_mocks(recording, preview=False)

    # pulumi.runtime.test waits for every resource registration to finish
    # before returning, so the mocks hold all of them once this returns.
    @pulumi.runtime.test
    def register():
        opts = pulumi.ResourceOptions()
        learn_ai, mitlearn = (
            keycloak.openid.Client(
                name,
                realm_id="olapps",
                client_id=client_id,
                access_type="CONFIDENTIAL",
                opts=opts,
            )
            for name, client_id in (
                ("learn-ai", LEARN_AI_CLIENT_ID),
                ("mitlearn", MITLEARN_CLIENT_ID),
            )
        )
        scopes = create_ol_memory_resources(
            realm_id=pulumi.Output.from_input("olapps"),
            learn_ai_client=learn_ai,
            resource_options=opts,
        )
        attach_memory_audience(
            "olapps-mitlearn-client-optional-scopes",
            realm_id="olapps",
            client=mitlearn,
            audience_scope=scopes.audience,
            resource_options=opts,
        )

    register()
    return recording


def _scope_id(mocks: _RecordingMocks, name: str) -> str:
    (scope,) = [
        s
        for s in mocks.of_type("keycloak:openid/clientScope:ClientScope")
        if s.inputs["name"] == name
    ]
    return f"{scope.name}_id"


@pytest.mark.parametrize(
    ("resource_name", "client"),
    [
        ("olapps-learn-ai-client-optional-scopes", "learn-ai"),
        ("olapps-mitlearn-client-optional-scopes", "mitlearn"),
    ],
)
def test_app_clients_get_the_audience_as_optional_and_keep_realm_defaults(
    mocks, resource_name, client
):
    """ClientOptionalScopes replaces the whole set, so the defaults must be in it."""
    optional = mocks.named(resource_name)
    assert optional.inputs["clientId"] == f"{client}_id"
    assert optional.inputs["optionalScopes"] == [
        *REALM_DEFAULT_OPTIONAL_SCOPES,
        AUDIENCE_SCOPE_NAME,
    ]


def test_only_the_admin_client_has_the_audience_by_default(mocks):
    """A default audience scope would make every session token a memory token."""
    defaults = mocks.of_type("keycloak:openid/clientDefaultScopes:ClientDefaultScopes")
    holders = [d for d in defaults if AUDIENCE_SCOPE_NAME in d.inputs["defaultScopes"]]
    assert [d.inputs["clientId"] for d in holders] == [
        "olapps-ol-memory-admin-client_id"
    ]


def test_no_client_holds_the_general_grant(mocks):
    """The general grant waits for its own service-account client."""
    for d in mocks.of_type("keycloak:openid/clientDefaultScopes:ClientDefaultScopes"):
        assert GENERAL_SCOPE_NAME not in d.inputs["defaultScopes"]
    for o in mocks.of_type("keycloak:openid/clientOptionalScopes:ClientOptionalScopes"):
        assert GENERAL_SCOPE_NAME not in o.inputs["optionalScopes"]


def test_audience_scope_names_the_service_and_adds_sub(mocks):
    """The olapps app clients lack the basic scope, so sub comes from here."""
    scope_id = _scope_id(mocks, AUDIENCE_SCOPE_NAME)
    (audience,) = [
        m
        for m in mocks.of_type(
            "keycloak:openid/audienceProtocolMapper:AudienceProtocolMapper"
        )
        if m.inputs["clientScopeId"] == scope_id
    ]
    assert audience.inputs["includedClientAudience"] == AUDIENCE_CLIENT_ID
    assert audience.inputs["addToAccessToken"] is True
    assert audience.inputs["addToIdToken"] is False

    (sub,) = mocks.of_type("keycloak:index/genericProtocolMapper:GenericProtocolMapper")
    assert sub.inputs["clientScopeId"] == scope_id
    assert sub.inputs["protocolMapper"] == "oidc-sub-mapper"
    assert sub.inputs["config"]["access.token.claim"] == "true"


def test_audience_client_is_bearer_only(mocks):
    """The service only verifies tokens; nothing logs in through it."""
    client = mocks.named("olapps-ol-memory-client")
    assert client.inputs["clientId"] == AUDIENCE_CLIENT_ID
    assert client.inputs["accessType"] == "BEARER-ONLY"


def test_learn_ai_audience_scope_names_learn_ai(mocks):
    """learn-ai can exchange only a token that names it as an audience."""
    scope_id = _scope_id(mocks, LEARN_AI_AUDIENCE_SCOPE_NAME)
    (audience,) = [
        m
        for m in mocks.of_type(
            "keycloak:openid/audienceProtocolMapper:AudienceProtocolMapper"
        )
        if m.inputs["clientScopeId"] == scope_id
    ]
    assert audience.inputs["includedClientAudience"] == LEARN_AI_CLIENT_ID
    assert audience.inputs["addToAccessToken"] is True


def test_admin_client_is_client_credentials_only(mocks):
    """The forget path has no learner, so no browser or password grant."""
    client = mocks.named("olapps-ol-memory-admin-client")
    assert client.inputs["clientId"] == ADMIN_CLIENT_ID
    assert client.inputs["accessType"] == "CONFIDENTIAL"
    assert client.inputs["serviceAccountsEnabled"] is True
    assert client.inputs["standardFlowEnabled"] is False
    assert client.inputs["implicitFlowEnabled"] is False
    assert client.inputs["directAccessGrantsEnabled"] is False

    defaults = mocks.named("olapps-ol-memory-admin-client-default-scopes")
    assert defaults.inputs["defaultScopes"] == ["basic", AUDIENCE_SCOPE_NAME]
    optional = mocks.named("olapps-ol-memory-admin-client-optional-scopes")
    assert optional.inputs["optionalScopes"] == []


def test_admin_credentials_go_to_vault(mocks):
    """The platform's forget job reads the admin client from Vault."""
    (secret,) = mocks.of_type("vault:generic/secret:Secret")
    assert secret.inputs["path"] == "secret-operations/sso/ol-memory-admin"
    # client_secret is a secret Output, so Pulumi marks the whole document
    # secret and the mock receives it wrapped.
    data = json.loads(secret.inputs["dataJson"]["value"])
    assert data == {
        "client_id": ADMIN_CLIENT_ID,
        "client_secret": "test-secret",  # pragma: allowlist secret
    }
