"""Learner-records clients must emit exactly what ol-analytics-api checks.

tenants/b2b_learner_records/auth.py grants an organization only when the
`learner_records_organizations` claim is a JSON array of UUID strings and the
`scope` string contains `learner-records:read`. Any other claim shape 403s every
request, so these tests pin the mapper type and the client's grant settings.
"""

import asyncio
import json

import pulumi
import pytest
from pydantic import ValidationError

# Python 3.14+ compatibility: ensure event loop exists for set_mocks()
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ol_infrastructure.substructure.keycloak.learner_records import (
    CONTRACT_END_DATE_CLAIM,
    LEARNER_RECORDS_READ_SCOPE,
    ORGANIZATIONS_CLAIM,
    LearnerRecordsClient,
    create_learner_records_clients,
    parse_learner_records_clients,
)

ORG_A = "8f14e45f-ceea-467a-9c1b-2f4b9c0a3d21"
ORG_B = "3e1a9c74-5b2d-4f88-9a01-7c6de2b4f019"


def _entry(**overrides: object) -> dict[str, object]:
    return {
        "name": "contoso-lms",
        "description": "Contoso LMS integration for Contoso Manufacturing",
        "organizations": [ORG_A],
        **overrides,
    }


class TestParseLearnerRecordsClients:
    """Stack config validation, which is the only review a contract gets."""

    def test_absent_config_means_no_clients(self):
        """An environment with no contracts has no config key at all."""
        assert parse_learner_records_clients(None) == []

    def test_claim_is_a_sorted_deduplicated_lowercase_json_array(self):
        """The claim value is stable however the UUIDs were written."""
        (client,) = parse_learner_records_clients(
            [_entry(organizations=[ORG_B.upper(), ORG_A, ORG_B])]
        )
        assert json.loads(client.organizations_claim_value) == sorted([ORG_A, ORG_B])

    def test_client_id_is_prefixed(self):
        """Client ids are namespaced away from the realm's other clients."""
        (client,) = parse_learner_records_clients([_entry()])
        assert client.client_id == "learner-records-contoso-lms"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"name": "Contoso LMS"},
            {"name": "contoso--lms"},
            {"name": ""},
            {"description": ""},
            {"organizations": []},
            {"organizations": ["contoso"]},
            {"contract_end_date": "next year"},
            {"read_pii": True},
        ],
    )
    def test_malformed_entry_is_rejected(self, overrides):
        """A bad entry fails the deploy instead of minting a useless client."""
        with pytest.raises(ValidationError):
            parse_learner_records_clients([_entry(**overrides)])

    def test_duplicate_names_are_rejected(self):
        """Two entries would collide on Pulumi resource names and Vault paths."""
        with pytest.raises(ValueError, match="contoso-lms"):
            parse_learner_records_clients([_entry(), _entry(organizations=[ORG_B])])


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


@pytest.fixture
def mocks():
    """Record every resource the helper registers."""
    recording = _RecordingMocks()
    pulumi.runtime.set_mocks(recording, preview=False)
    return recording


def _create(clients: list[LearnerRecordsClient]) -> None:
    # pulumi.runtime.test waits for every resource registration to finish
    # before returning, so the mocks hold all of them once this returns.
    @pulumi.runtime.test
    def register():
        create_learner_records_clients(
            realm_id="olapps",
            realm_name="olapps",
            api_client_id="ol-analytics-api-client",
            clients=clients,
            keycloak_url="https://sso-qa.ol.mit.edu",
            opts=pulumi.ResourceOptions(),
        )

    register()


def test_client_is_client_credentials_only(mocks):
    """No browser or password grant, and a pinned revocation window."""
    _create(parse_learner_records_clients([_entry()]))

    (client,) = mocks.of_type("keycloak:openid/client:Client")
    assert client.inputs["clientId"] == "learner-records-contoso-lms"
    assert client.inputs["serviceAccountsEnabled"] is True
    assert client.inputs["standardFlowEnabled"] is False
    assert client.inputs["implicitFlowEnabled"] is False
    assert client.inputs["directAccessGrantsEnabled"] is False
    assert client.inputs["accessTokenLifespan"] == "300"

    (defaults,) = mocks.of_type(
        "keycloak:openid/clientDefaultScopes:ClientDefaultScopes"
    )
    assert defaults.inputs["defaultScopes"] == [
        "basic",
        LEARNER_RECORDS_READ_SCOPE,
    ]


def test_organizations_claim_is_json_typed_and_access_token_only(mocks):
    """A String-typed claim reaches the API as a string and grants nothing."""
    _create(parse_learner_records_clients([_entry(organizations=[ORG_A, ORG_B])]))

    (mapper,) = mocks.of_type(
        "keycloak:openid/hardcodedClaimProtocolMapper:HardcodedClaimProtocolMapper"
    )
    assert mapper.inputs["claimName"] == ORGANIZATIONS_CLAIM
    assert mapper.inputs["claimValueType"] == "JSON"
    assert json.loads(mapper.inputs["claimValue"]) == sorted([ORG_A, ORG_B])
    assert mapper.inputs["addToAccessToken"] is True
    assert mapper.inputs["addToIdToken"] is False
    assert mapper.inputs["addToUserinfo"] is False


def test_contract_end_date_claim_only_when_set(mocks):
    """The end date is optional per contract."""
    _create(
        parse_learner_records_clients(
            [
                _entry(),
                _entry(name="fabrikam-lms", contract_end_date="2027-06-30"),
            ]
        )
    )

    mappers = mocks.of_type(
        "keycloak:openid/hardcodedClaimProtocolMapper:HardcodedClaimProtocolMapper"
    )
    end_dates = [
        m.inputs for m in mappers if m.inputs["claimName"] == CONTRACT_END_DATE_CLAIM
    ]
    assert len(end_dates) == 1
    assert end_dates[0]["claimValue"] == "2027-06-30"


def test_read_scope_adds_the_api_audience(mocks):
    """APISIX rejects learner-records tokens not addressed to the API."""
    _create([])

    (scope,) = mocks.of_type("keycloak:openid/clientScope:ClientScope")
    assert scope.inputs["name"] == LEARNER_RECORDS_READ_SCOPE
    assert scope.inputs["includeInTokenScope"] is True
    (audience,) = mocks.of_type(
        "keycloak:openid/audienceProtocolMapper:AudienceProtocolMapper"
    )
    assert audience.inputs["includedClientAudience"] == "ol-analytics-api-client"
    assert audience.inputs["addToAccessToken"] is True
    assert mocks.of_type("keycloak:openid/client:Client") == []


def test_vault_secret_carries_what_the_partner_needs(mocks):
    """Operators hand the partner this document as-is."""
    _create(parse_learner_records_clients([_entry()]))

    (secret,) = mocks.of_type("vault:generic/secret:Secret")
    assert secret.inputs["path"] == (
        "secret-operations/sso/learner-records/contoso-lms"
    )
    # client_secret is a secret Output, so Pulumi marks the whole document
    # secret and the mock receives it wrapped.
    data = json.loads(secret.inputs["dataJson"]["value"])
    assert data["client_id"] == "learner-records-contoso-lms"
    assert data["token_url"] == (
        "https://sso-qa.ol.mit.edu/realms/olapps/protocol/openid-connect/token"  # noqa: S105
    )
    assert data["scope"] == LEARNER_RECORDS_READ_SCOPE
    assert data["organizations"] == [ORG_A]
