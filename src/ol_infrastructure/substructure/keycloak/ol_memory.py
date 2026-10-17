"""Keycloak side of the shared memory service (the Hindsight stack).

An app acts on a learner's memory with a token for the memory service's
audience, normally obtained by standard token exchange (RFC 8693) from that
learner's session token, requesting the audience scope. The service takes the
learner from the token's ``sub`` and the app from its ``azp``. Two
client-credentials grants sit beside that: the general scope, for memory that
belongs to no learner, and the admin client, for the forget path on account
deletion.

The audience scope has to stay *optional* on app clients. As a default scope it
would put the memory audience on every session token. Optional, it reaches a
token only when the request asks for it: by exchange, or by an app's own login
asking for the scope. Both take the app client's secret, and neither reaches
past the token's own learner.
"""

from dataclasses import dataclass

import pulumi_keycloak as keycloak
import pulumi_vault as vault
from pulumi import Input, Output, ResourceOptions

AUDIENCE_CLIENT_ID = "ol-memory-client"
AUDIENCE_SCOPE_NAME = "ol-memory-audience"
GENERAL_SCOPE_NAME = "ol-memory-general"
ADMIN_CLIENT_ID = "ol-memory-admin-client"
LEARN_AI_AUDIENCE_SCOPE_NAME = "ol-learn-ai-audience"

# What the olapps app clients carry as optional scopes today (Keycloak's realm
# defaults, read from QA on 2026-10-16). ClientOptionalScopes replaces the whole
# set, so these are kept alongside the memory audience.
REALM_DEFAULT_OPTIONAL_SCOPES = [
    "address",
    "phone",
    "offline_access",
    "microprofile-jwt",
]


@dataclass(frozen=True)
class OLMemoryScopes:
    """The client scopes app clients attach to take part in memory."""

    audience: keycloak.openid.ClientScope
    learn_ai_audience: keycloak.openid.ClientScope


def create_ol_memory_resources(
    *,
    realm_id: Output[str],
    learn_ai_client: keycloak.openid.Client,
    resource_options: ResourceOptions,
) -> OLMemoryScopes:
    """Create the memory service's audience, scopes and admin client.

    :param realm_id: The olapps realm.
    :param learn_ai_client: ol-learn-ai-client, named as an audience on the
        tokens of the client learn-ai's routes log learners in through.
    :param resource_options: Options carrying the Keycloak provider.
    :returns: The scopes app clients attach.
    :rtype: OLMemoryScopes
    """
    audience_client = keycloak.openid.Client(
        "olapps-ol-memory-client",
        name=AUDIENCE_CLIENT_ID,
        realm_id=realm_id,
        client_id=AUDIENCE_CLIENT_ID,
        enabled=True,
        access_type="BEARER-ONLY",
        opts=resource_options,
    )

    audience_scope = keycloak.openid.ClientScope(
        "olapps-ol-memory-audience-scope",
        name=AUDIENCE_SCOPE_NAME,
        realm_id=realm_id,
        description="Request a token for the shared memory service",
        include_in_token_scope=True,
        opts=resource_options,
    )
    keycloak.openid.AudienceProtocolMapper(
        "olapps-ol-memory-audience-mapper",
        name="ol-memory-audience",
        realm_id=realm_id,
        client_scope_id=audience_scope.id,
        included_client_audience=audience_client.client_id,
        add_to_access_token=True,
        add_to_id_token=False,
        opts=resource_options,
    )
    # The olapps app clients predate Keycloak 25's basic scope and do not carry
    # it, so their tokens have no sub, and the service takes the learner from
    # sub. Adding it here puts sub on memory tokens only, leaving every session
    # token as it is.
    keycloak.GenericProtocolMapper(
        "olapps-ol-memory-sub-mapper",
        name="sub",
        realm_id=realm_id,
        client_scope_id=audience_scope.id,
        protocol="openid-connect",
        protocol_mapper="oidc-sub-mapper",
        config={
            "access.token.claim": "true",
            "introspection.token.claim": "true",
        },
        opts=resource_options,
    )

    # The general-memory grant. The service honours it only on a
    # client-credentials token, so a learner token never writes general memory
    # whatever scopes it carries.
    keycloak.openid.ClientScope(
        "olapps-ol-memory-general-scope",
        name=GENERAL_SCOPE_NAME,
        realm_id=realm_id,
        description="Write general (non-learner) memory",
        include_in_token_scope=True,
        opts=resource_options,
    )

    # learn-ai's routes log learners in through ol-mitlearn-client, so learn-ai
    # holds tokens issued to that client. Keycloak lets a client exchange only a
    # token it is the azp or an audience of, so mit-learn's tokens name learn-ai
    # too, and learn-ai exchanges as itself.
    learn_ai_audience_scope = keycloak.openid.ClientScope(
        "olapps-ol-learn-ai-audience-scope",
        name=LEARN_AI_AUDIENCE_SCOPE_NAME,
        realm_id=realm_id,
        description="Name learn-ai as an audience, so it can exchange the token",
        include_in_token_scope=False,
        opts=resource_options,
    )
    keycloak.openid.AudienceProtocolMapper(
        "olapps-ol-learn-ai-audience-mapper",
        name="ol-learn-ai-audience",
        realm_id=realm_id,
        client_scope_id=learn_ai_audience_scope.id,
        included_client_audience=learn_ai_client.client_id,
        add_to_access_token=True,
        add_to_id_token=False,
        opts=resource_options,
    )

    # The forget path on account deletion, when no learner token exists.
    admin_client = keycloak.openid.Client(
        "olapps-ol-memory-admin-client",
        name=ADMIN_CLIENT_ID,
        realm_id=realm_id,
        client_id=ADMIN_CLIENT_ID,
        enabled=True,
        access_type="CONFIDENTIAL",
        standard_flow_enabled=False,
        implicit_flow_enabled=False,
        direct_access_grants_enabled=False,
        service_accounts_enabled=True,
        valid_redirect_uris=[],
        opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
    )
    keycloak.openid.ClientDefaultScopes(
        "olapps-ol-memory-admin-client-default-scopes",
        realm_id=realm_id,
        client_id=admin_client.id,
        default_scopes=["basic", audience_scope.name],
        opts=resource_options,
    )
    keycloak.openid.ClientOptionalScopes(
        "olapps-ol-memory-admin-client-optional-scopes",
        realm_id=realm_id,
        client_id=admin_client.id,
        optional_scopes=[],
        opts=resource_options,
    )
    vault.generic.Secret(
        "olapps-ol-memory-admin-client-vault-credentials",
        path="secret-operations/sso/ol-memory-admin",
        data_json=Output.json_dumps(
            {
                "client_id": admin_client.client_id,
                "client_secret": admin_client.client_secret,
            }
        ),
    )

    attach_memory_audience(
        "olapps-learn-ai-client-optional-scopes",
        realm_id=realm_id,
        client=learn_ai_client,
        audience_scope=audience_scope,
        resource_options=resource_options,
    )

    return OLMemoryScopes(
        audience=audience_scope, learn_ai_audience=learn_ai_audience_scope
    )


def attach_memory_audience(
    resource_name: str,
    *,
    realm_id: Input[str],
    client: keycloak.openid.Client,
    audience_scope: keycloak.openid.ClientScope,
    resource_options: ResourceOptions,
) -> keycloak.openid.ClientOptionalScopes:
    """Offer the memory audience to an app client as an optional scope.

    :param resource_name: Pulumi name of the client's optional-scope set.
    :param realm_id: The client's realm.
    :param client: The app client.
    :param audience_scope: The memory audience scope.
    :param resource_options: Options carrying the Keycloak provider.
    :returns: The client's optional-scope set.
    :rtype: keycloak.openid.ClientOptionalScopes
    """
    return keycloak.openid.ClientOptionalScopes(
        resource_name,
        realm_id=realm_id,
        client_id=client.id,
        optional_scopes=[*REALM_DEFAULT_OPTIONAL_SCOPES, audience_scope.name],
        # Deleting it would detach every listed scope, the realm defaults
        # included.
        opts=resource_options.merge(ResourceOptions(retain_on_delete=True)),
    )
