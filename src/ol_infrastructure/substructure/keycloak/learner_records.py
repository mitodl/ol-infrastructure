"""Keycloak clients for ol-analytics-api's learner-records tenant.

Access to learner records is settled when a contract is signed, not at request
time (ol-analytics-api docs/b2b-learner-records-provider-authorization.md). MIT
issues one client-credentials client per contracted integration, and the client
carries the contract's terms: the organizations it may read, as a hardcoded
claim the API checks the request path against. The reviewed change that adds a
client here is MIT's record of that access, and removing it revokes the
credential.

Each client is one entry in the keycloak stack's
``keycloak_realm:olapps-learner-records-clients`` config::

    keycloak_realm:olapps-learner-records-clients:
      - name: contoso-lms
        description: Contoso LMS integration for Contoso Manufacturing
        organizations:
          - 8f14e45f-ceea-467a-9c1b-2f4b9c0a3d21
        contract_end_date: "2027-06-30"

Organization UUIDs are the Keycloak organization ids, which differ between QA
and Production, so the entries live in each environment's stack config.
"""

import json
from datetime import date
from functools import partial
from typing import Annotated
from uuid import UUID

import pulumi_keycloak as keycloak
import pulumi_vault as vault
from pulumi import Input, Output, ResourceOptions
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

LEARNER_RECORDS_READ_SCOPE = "learner-records:read"
# ol-analytics-api tenants/b2b_learner_records/auth.py reads this claim and
# grants nothing unless it is a JSON array of UUID strings.
ORGANIZATIONS_CLAIM = "learner_records_organizations"
# ol-analytics-api tenants/b2b_learner_records/token.py (mitodl/ol-analytics-api#73)
# refuses the client's
# tokens once this date has passed (inclusive, end of day Anywhere on Earth).
# That is a backstop; the client should still be removed at contract end.
CONTRACT_END_DATE_CLAIM = "learner_records_contract_end_date"
# A token issued before its client is removed stays valid until it expires, so
# this is the revocation window. Pinned on each client rather than inherited
# from the realm so a realm-wide change can't widen it unnoticed.
ACCESS_TOKEN_LIFESPAN_SECONDS = 300
# Keycloak limits client_id to 255 characters; this leaves room for the prefix.
MAX_CLIENT_NAME_LENGTH = 64
CLIENT_ID_PREFIX = "learner-records-"
VAULT_PATH_PREFIX = "secret-operations/sso/learner-records"


class LearnerRecordsClient(BaseModel):
    """The contract terms one learner-records client encodes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[
        str,
        StringConstraints(
            pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=MAX_CLIENT_NAME_LENGTH
        ),
    ]
    description: Annotated[str, StringConstraints(min_length=1)]
    organizations: Annotated[list[UUID], Field(min_length=1)]
    contract_end_date: date | None = None

    @property
    def client_id(self) -> str:
        """Return the Keycloak client_id, which the API logs as the caller."""
        return f"{CLIENT_ID_PREFIX}{self.name}"

    @property
    def sorted_organizations(self) -> list[str]:
        """Return the organizations as sorted, de-duplicated UUID strings."""
        return sorted({str(org) for org in self.organizations})

    @property
    def organizations_claim_value(self) -> str:
        """Return the organizations as the JSON array the claim must carry."""
        return json.dumps(self.sorted_organizations)


_client_list_adapter = TypeAdapter(list[LearnerRecordsClient])


def _credentials_document(
    contract_fields: dict[str, object], credentials: dict[str, str]
) -> str:
    return json.dumps({**credentials, **contract_fields})


def parse_learner_records_clients(
    raw_clients: list[dict[str, object]] | None,
) -> list[LearnerRecordsClient]:
    """Validate the stack config entries for learner-records clients.

    An absent config key means no contracts in that environment yet.

    :param raw_clients: The ``olapps-learner-records-clients`` config value.
    :returns: One validated entry per client.
    :rtype: list[LearnerRecordsClient]
    :raises ValueError: An entry is malformed, or two entries share a name.
    """
    clients = _client_list_adapter.validate_python(raw_clients or [])
    names = [client.name for client in clients]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        msg = f"Duplicate learner-records client names: {', '.join(duplicates)}"
        raise ValueError(msg)
    return clients


def create_learner_records_clients(  # noqa: PLR0913
    realm_id: Input[str],
    realm_name: str,
    api_client_id: Input[str],
    clients: list[LearnerRecordsClient],
    keycloak_url: str,
    opts: ResourceOptions,
) -> None:
    """Create the learner-records scope and one client per contract.

    :param realm_id: The realm the clients live in.
    :param realm_name: The realm's name, for the token URL stored in Vault.
    :param api_client_id: The client_id of ol-analytics-api's own client. APISIX
        requires it in the token audience on the learner-records route.
    :param clients: The contract terms, one entry per client.
    :param keycloak_url: The Keycloak base URL.
    :param opts: Resource options carrying the Keycloak provider.
    """
    read_scope = keycloak.openid.ClientScope(
        "olapps-learner-records-read-client-scope",
        realm_id=realm_id,
        name=LEARNER_RECORDS_READ_SCOPE,
        description="Read B2B learner records from ol-analytics-api",
        include_in_token_scope=True,
        opts=opts,
    )
    # Every client in the realm is signed by the same key, so without an
    # audience any olapps token would pass the gateway's signature check. The
    # mapper sits on the scope, so a token that carries the scope is also
    # addressed to the API.
    keycloak.openid.AudienceProtocolMapper(
        "olapps-learner-records-read-audience-mapper",
        realm_id=realm_id,
        client_scope_id=read_scope.id,
        name="ol-analytics-api-audience",
        included_client_audience=api_client_id,
        add_to_access_token=True,
        add_to_id_token=False,
        opts=opts,
    )

    issuer_url = f"{keycloak_url}/realms/{realm_name}"
    for contract in clients:
        client = keycloak.openid.Client(
            f"olapps-{contract.client_id}-client",
            name=contract.client_id,
            realm_id=realm_id,
            client_id=contract.client_id,
            description=contract.description,
            enabled=True,
            access_type="CONFIDENTIAL",
            standard_flow_enabled=False,
            implicit_flow_enabled=False,
            direct_access_grants_enabled=False,
            service_accounts_enabled=True,
            valid_redirect_uris=[],
            access_token_lifespan=str(ACCESS_TOKEN_LIFESPAN_SECONDS),
            opts=opts.merge(ResourceOptions(delete_before_replace=True)),
        )
        # Replaces the realm defaults Keycloak attaches to a new client, so the
        # token carries sub (basic) and the read scope and nothing about the
        # service-account user. That also drops the service_account scope and
        # its client_id claim on purpose: azp carries the same value, and the
        # API logs azp first.
        keycloak.openid.ClientDefaultScopes(
            f"olapps-{contract.client_id}-client-default-scopes",
            realm_id=realm_id,
            client_id=client.id,
            default_scopes=["basic", read_scope.name],
            opts=opts,
        )
        keycloak.openid.ClientOptionalScopes(
            f"olapps-{contract.client_id}-client-optional-scopes",
            realm_id=realm_id,
            client_id=client.id,
            optional_scopes=[],
            opts=opts,
        )
        keycloak.openid.HardcodedClaimProtocolMapper(
            f"olapps-{contract.client_id}-organizations-mapper",
            realm_id=realm_id,
            client_id=client.id,
            name="learner-records-organizations",
            claim_name=ORGANIZATIONS_CLAIM,
            claim_value=contract.organizations_claim_value,
            # Anything but JSON arrives as a string, which the API rejects.
            claim_value_type="JSON",
            add_to_access_token=True,
            add_to_id_token=False,
            add_to_userinfo=False,
            opts=opts,
        )
        if contract.contract_end_date is not None:
            keycloak.openid.HardcodedClaimProtocolMapper(
                f"olapps-{contract.client_id}-contract-end-date-mapper",
                realm_id=realm_id,
                client_id=client.id,
                name="learner-records-contract-end-date",
                claim_name=CONTRACT_END_DATE_CLAIM,
                claim_value=contract.contract_end_date.isoformat(),
                claim_value_type="String",
                add_to_access_token=True,
                add_to_id_token=False,
                add_to_userinfo=False,
                opts=opts,
            )
        # What gets handed to the partner. Nothing in the cluster reads it.
        vault.generic.Secret(
            f"olapps-{contract.client_id}-vault-credentials",
            path=f"{VAULT_PATH_PREFIX}/{contract.name}",
            data_json=Output.all(
                client_id=client.client_id,
                client_secret=client.client_secret,
            ).apply(
                partial(
                    _credentials_document,
                    {
                        "issuer": issuer_url,
                        "token_url": f"{issuer_url}/protocol/openid-connect/token",
                        "scope": LEARNER_RECORDS_READ_SCOPE,
                        "organizations": contract.sorted_organizations,
                        "contract_end_date": (
                            contract.contract_end_date.isoformat()
                            if contract.contract_end_date
                            else None
                        ),
                    },
                )
            ),
        )
