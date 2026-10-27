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

ol-data-platform's evaluation gate (ol-llm's evaluation-gate.yml) runs on GitHub
Actions with a client of its own, so the credentials in GitHub can be revoked
without rotating every application's. Only the Production Vault holds them; a person
copies them into that repository's secrets.
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
OPIK_EVALUATION_CLIENT_ID = "ol-data-platform-evaluation-opik"
OPIK_EVALUATION_VAULT_PATH = "evaluation-ol-data-platform"


def _opik_sdk_client(
    resource_prefix: str, client_id: str, keycloak_provider: keycloak.Provider
) -> keycloak.openid.Client:
    """Declare a client-credentials-only client whose tokens the Opik gateway admits.

    :param resource_prefix: Prefix of the Pulumi resource names.
    :param client_id: The Keycloak client id.
    :param keycloak_provider: Provider for the Production Keycloak instance.
    :returns: The client.
    :rtype: keycloak.openid.Client
    """
    client = keycloak.openid.Client(
        f"{resource_prefix}-client",
        name=f"{resource_prefix}-client",
        realm_id=REALM_NAME,
        client_id=client_id,
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
    # 403.
    keycloak.openid.AudienceProtocolMapper(
        f"{resource_prefix}-audience-mapper",
        realm_id=REALM_NAME,
        client_id=client.id,
        name="opik-audience",
        included_client_audience=OPIK_GATEWAY_CLIENT_ID,
        add_to_access_token=True,
        add_to_id_token=False,
        opts=ResourceOptions(provider=keycloak_provider),
    )
    return client


def _credentials_json(client: keycloak.openid.Client, keycloak_url: str) -> Output[str]:
    return Output.all(
        url=f"{keycloak_url}/realms/{REALM_NAME}",
        client_id=client.client_id,
        client_secret=client.client_secret,
    ).apply(json.dumps)


def create_opik_sdk_resources(
    keycloak_provider: keycloak.Provider,
    keycloak_url: str,
) -> None:
    """Declare the Opik SDK clients and their secrets in the environments' Vaults.

    :param keycloak_provider: Provider for the Production Keycloak instance.
    :param keycloak_url: Base URL of the Production Keycloak instance.
    """
    opik_sdk_client = _opik_sdk_client(
        "ol-platform-engineering-opik-sdk", OPIK_SDK_CLIENT_ID, keycloak_provider
    )
    credentials_json = _credentials_json(opik_sdk_client, keycloak_url)

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
        if env_suffix == "production":
            evaluation_client = _opik_sdk_client(
                "ol-platform-engineering-opik-evaluation-ol-data-platform",
                OPIK_EVALUATION_CLIENT_ID,
                keycloak_provider,
            )
            vault.generic.Secret(
                "ol-platform-engineering-opik-evaluation-ol-data-platform-vault-credentials",
                path=f"{OPIK_SDK_VAULT_MOUNT}/{OPIK_EVALUATION_VAULT_PATH}",
                data_json=_credentials_json(evaluation_client, keycloak_url),
                opts=ResourceOptions(provider=vault_provider, depends_on=[opik_mount]),
            )
