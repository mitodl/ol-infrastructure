"""Keycloak clients that Opik SDKs authenticate with, one per app and environment.

Every environment sends its traces to the Production Opik (see lib/opik.py), whose
gateway verifies bearer tokens against the Production ``ol-platform-engineering``
realm's JWKS. A CI or QA application therefore needs a Production client, so this
module only runs on the Production stack. It declares ``opik-<app>-<env>`` for every
application and environment in OPIK_APP_ENVIRONMENTS and writes each to that
environment's Vault, so revoking one touches no other app or environment.

The clients are separate from ``ol-opik-client``, which also backs the Opik UI login
and whose Vault secret carries the gateway's session secret. They have only the
client-credentials grant, so the CI and QA Vaults never hold the UI client, and they
can be rotated without signing anyone out of the UI.

ol-data-platform's evaluation gate (ol-llm's evaluation-gate.yml) runs on GitHub
Actions with a client of its own too. Only the Production Vault holds it, and this
module writes it, with the Opik URL and the gate's AWS role, to that repository's
Actions secrets and variables.
"""

import json

import pulumi_github as github
import pulumi_keycloak as keycloak
import pulumi_vault as vault
from pulumi import Output, ResourceOptions

from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.github_helper import get_github_provider
from ol_infrastructure.lib.opik import (
    OPIK_APP_ENVIRONMENTS,
    OPIK_SDK_ENVIRONMENTS,
    OPIK_SDK_VAULT_MOUNT,
    opik_api_url,
    opik_sdk_client_id,
    opik_sdk_vault_path,
)
from ol_infrastructure.lib.pulumi_helper import make_stack_reference
from ol_infrastructure.lib.vault import get_vault_provider

REALM_NAME = "ol-platform-engineering"
# The client the Opik gateway's OIDC plugin is configured with; its bearer route
# admits a token only when this is in aud.
OPIK_GATEWAY_CLIENT_ID = "ol-opik-client"
OPIK_EVALUATION_CLIENT_ID = "ol-data-platform-evaluation-opik"
OPIK_EVALUATION_VAULT_PATH = "evaluation-ol-data-platform"
OPIK_EVALUATION_REPOSITORY = "ol-data-platform"


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


def _evaluation_gate_github_settings(
    evaluation_client: keycloak.openid.Client, keycloak_url: str
) -> None:
    """Write what ol-data-platform's evaluation-gate.yaml reads to its Actions settings.

    :param evaluation_client: The gate's Keycloak client.
    :param keycloak_url: Base URL of the Production Keycloak instance.
    """
    github_opts = ResourceOptions(provider=get_github_provider("mitodl"))
    iam_stack = make_stack_reference(projects.IAM, "default")
    variables: dict[str, str | Output[str]] = {
        "OPIK_URL": opik_api_url(),
        "OPIK_KEYCLOAK_TOKEN_URL": (
            f"{keycloak_url}/realms/{REALM_NAME}/protocol/openid-connect/token"
        ),
        "EVALUATION_AWS_ROLE_ARN": iam_stack.require_output(
            "ol_data_platform_evaluation_gate_role_arn"
        ),
    }
    for name, value in variables.items():
        github.ActionsVariable(
            f"{OPIK_EVALUATION_REPOSITORY}-actions-variable-{name.lower()}",
            repository=OPIK_EVALUATION_REPOSITORY,
            variable_name=name,
            value=value,
            opts=github_opts,
        )
    secrets = {
        "OPIK_EVALUATION_CLIENT_ID": evaluation_client.client_id,
        "OPIK_EVALUATION_CLIENT_SECRET": evaluation_client.client_secret,
    }
    for name, value in secrets.items():
        github.ActionsSecret(
            f"{OPIK_EVALUATION_REPOSITORY}-actions-secret-{name.lower()}",
            repository=OPIK_EVALUATION_REPOSITORY,
            secret_name=name,
            value=value,
            opts=github_opts,
        )


def create_opik_sdk_resources(
    keycloak_provider: keycloak.Provider,
    keycloak_url: str,
) -> None:
    """Declare the Opik SDK clients and their secrets in the environments' Vaults.

    :param keycloak_provider: Provider for the Production Keycloak instance.
    :param keycloak_url: Base URL of the Production Keycloak instance.
    """
    for env_suffix in OPIK_SDK_ENVIRONMENTS:
        vault_provider = get_vault_provider(
            f"https://vault-{env_suffix}.odl.mit.edu",
            f"operations.{env_suffix}",
            provider_name=f"vault-provider-{env_suffix}",
        )
        opik_mount = vault.Mount(
            f"opik-sdk-vault-mount-{env_suffix}",
            description="Production Keycloak clients Opik SDKs authenticate with",
            path=OPIK_SDK_VAULT_MOUNT,
            type="kv",
            options={"version": "1"},
            # Vault refuses a second mount at the same path, so a replacement
            # has to remove the old one first.
            opts=ResourceOptions(provider=vault_provider, delete_before_replace=True),
        )
        for app, app_environments in OPIK_APP_ENVIRONMENTS.items():
            if env_suffix not in app_environments:
                continue
            client_id = opik_sdk_client_id(app, env_suffix)
            client = _opik_sdk_client(
                f"ol-platform-engineering-{client_id}", client_id, keycloak_provider
            )
            vault.generic.Secret(
                f"ol-platform-engineering-{client_id}-vault-credentials",
                path=f"{OPIK_SDK_VAULT_MOUNT}/{opik_sdk_vault_path(app)}",
                data_json=_credentials_json(client, keycloak_url),
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
            _evaluation_gate_github_settings(evaluation_client, keycloak_url)
