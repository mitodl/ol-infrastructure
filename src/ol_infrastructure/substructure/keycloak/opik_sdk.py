"""Keycloak client that Opik SDKs in every environment authenticate with.

Every environment sends its traces to the Production Opik (see lib/opik.py), whose
gateway verifies bearer tokens against the Production ``ol-platform-engineering``
realm's JWKS. A CI or QA application therefore needs a Production client, so this
module only runs on the Production stack and writes the client's credentials to the
CI, QA, and Production Vaults.

The client is separate from ``ol-opik-client``, which also backs the Opik UI login and
whose Vault secret carries the gateway's session secret. This one has only the
client-credentials grant, so the CI and QA Vaults never hold the UI client, and it
can be rotated without signing anyone out of the UI.
"""

import json

import pulumi_keycloak as keycloak
import pulumi_vault as vault
from pulumi import Output, ResourceOptions

from ol_infrastructure.lib.opik import (
    OPIK_SDK_ENVIRONMENTS,
    OPIK_SDK_VAULT_MOUNT,
    OPIK_SDK_VAULT_PATH,
)
from ol_infrastructure.lib.vault import get_vault_provider

REALM_NAME = "ol-platform-engineering"
OPIK_SDK_CLIENT_ID = "ol-opik-sdk"
# The client the Opik gateway's OIDC plugin is configured with; its bearer route
# admits a token only when this is in aud.
OPIK_GATEWAY_CLIENT_ID = "ol-opik-client"


def create_opik_sdk_resources(
    keycloak_provider: keycloak.Provider,
    keycloak_url: str,
) -> None:
    """Declare the Opik SDK client and its secret in each environment's Vault.

    :param keycloak_provider: Provider for the Production Keycloak instance.
    :param keycloak_url: Base URL of the Production Keycloak instance.
    """
    opik_sdk_client = keycloak.openid.Client(
        "ol-platform-engineering-opik-sdk-client",
        name="ol-platform-engineering-opik-sdk-client",
        realm_id=REALM_NAME,
        client_id=OPIK_SDK_CLIENT_ID,
        enabled=True,
        access_type="CONFIDENTIAL",
        service_accounts_enabled=True,
        standard_flow_enabled=False,
        implicit_flow_enabled=False,
        direct_access_grants_enabled=False,
        opts=ResourceOptions(provider=keycloak_provider, delete_before_replace=True),
    )
    # Every client in the realm signs with the same key, so the gateway's audience
    # check (match_with_client_id on ol-opik-client) is what tells an SDK token
    # from another client's. Without ol-opik-client in aud, that check returns
    # 403 to every SDK.
    keycloak.openid.AudienceProtocolMapper(
        "ol-platform-engineering-opik-sdk-audience-mapper",
        realm_id=REALM_NAME,
        client_id=opik_sdk_client.id,
        name="opik-audience",
        included_client_audience=OPIK_GATEWAY_CLIENT_ID,
        add_to_access_token=True,
        add_to_id_token=False,
        opts=ResourceOptions(provider=keycloak_provider),
    )
    credentials_json = Output.all(
        url=f"{keycloak_url}/realms/{REALM_NAME}",
        client_id=opik_sdk_client.client_id,
        client_secret=opik_sdk_client.client_secret,
    ).apply(json.dumps)

    for env_suffix in OPIK_SDK_ENVIRONMENTS:
        vault_provider = get_vault_provider(
            f"https://vault-{env_suffix}.odl.mit.edu",
            f"operations.{env_suffix}",
            provider_name=f"vault-provider-{env_suffix}",
        )
        opik_mount = vault.Mount(
            f"opik-sdk-vault-mount-{env_suffix}",
            description="Production Keycloak client Opik SDKs authenticate with",
            path=OPIK_SDK_VAULT_MOUNT,
            type="kv",
            options={"version": "1"},
            # Vault refuses a second mount at the same path, so a replacement
            # has to remove the old one first.
            opts=ResourceOptions(provider=vault_provider, delete_before_replace=True),
        )
        vault.generic.Secret(
            f"ol-platform-engineering-opik-sdk-client-vault-credentials-{env_suffix}",
            path=f"{OPIK_SDK_VAULT_MOUNT}/{OPIK_SDK_VAULT_PATH}",
            data_json=credentials_json,
            opts=ResourceOptions(provider=vault_provider, depends_on=[opik_mount]),
        )
