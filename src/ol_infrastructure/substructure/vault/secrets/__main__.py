import json
from pathlib import Path

import pulumi_vault as vault
from pulumi import Config, ResourceOptions

from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

stack_info = parse_stack()
global_vault_secrets = read_yaml_secrets(
    Path(f"vault/secrets.{stack_info.env_suffix}.yaml")
)
grafana_vault_secrets = read_yaml_secrets(
    Path(f"alloy/grafana.{stack_info.env_suffix}.yaml")
)
if Config("vault_server").get("env_namespace"):
    setup_vault_provider()
keycloak_config = Config("keycloak")
vault_config = Config("vault")

# Create the secret mount used for storing global secrets
global_vault_mount = vault.Mount(
    "global-secrets-mount",
    path="secret-global",
    type="kv",
    options={"version": 2},
    description="Storage of global secrets used across our applications.",
    opts=ResourceOptions(delete_before_replace=True),
)

for key, data in global_vault_secrets.items():
    vault.kv.SecretV2(
        f"global-vault-secrets-{key}",
        name=key,
        mount=global_vault_mount,
        data_json=json.dumps(data),
    )

# Configure secret-dev mount and keycloak auth
if "QA" in stack_info.name:
    # Create the secret mount used for storing env secrets for developers
    dev_vault_mount = vault.Mount(
        f"ol-dev-configuration-secrets-mount-{stack_info.env_suffix}",
        path="secret-dev",
        type="kv-v2",
        options={"version": 2},
        description="Storage of configuration secrets used by Devs",
        opts=ResourceOptions(delete_before_replace=True),
    )

    # Create the secret mount used for sharing secrets
    dev_sandbox_vault_mount = vault.Mount(
        f"ol-dev-sharing-secrets-mount-{stack_info.env_suffix}",
        path="secret-sandbox",
        type="kv-v2",
        options={"version": 2},
        description="Securely share secrets",
        opts=ResourceOptions(delete_before_replace=True),
    )

    # Read MIT Open vault secrets
    mitopen_vault_secrets = read_yaml_secrets(
        Path(f"mitopen/secrets.{stack_info.env_suffix}.yaml"),
    )

    vault.generic.Secret(
        f"ol-dev-configuration-secrets-{stack_info.env_suffix}",
        path=dev_vault_mount.path.apply("{}/mitopen/secrets".format),
        data_json=json.dumps(mitopen_vault_secrets),
    )

    # Enable OIDC auth method and configure it with Keycloak
    vault_oidc_keycloak_auth = vault.jwt.AuthBackend(
        "vault-oidc-keycloak-backend",
        path="oidc",
        type="oidc",
        description="OIDC auth Keycloak integration for use with dev vault client cli",
        oidc_discovery_url=f"{keycloak_config.get('url')}/realms/ol-platform-engineering",
        oidc_client_id=f"{keycloak_config.get('client_id')}",
        oidc_client_secret=f"{keycloak_config.get('client_secret')}",
        default_role="local-developer",
        opts=ResourceOptions(delete_before_replace=True),
    )

    # Local Developer policy definition
    local_developer_policy = vault.Policy(
        "local-developer-policy",
        name="local-developer",
        policy=(Path(__file__).resolve())
        .parent.parent.joinpath("policies/developer/local_developer_policy.hcl")
        .read_text(),
    )

    # Configure OIDC role
    local_dev_role = vault.jwt.AuthBackendRole(
        "local-dev-role",
        backend=vault_oidc_keycloak_auth.path,
        role_name="local-dev",
        token_policies=[
            local_developer_policy.name,
        ],
        allowed_redirect_uris=[
            "http://localhost:8250/oidc/callback",
            f"{vault_config.get('address')}/ui/vault/auth/oidc/oidc/callback",
        ],
        bound_audiences=[keycloak_config.get("client_id")],
        user_claim="sub",
        role_type="oidc",
    )

vault.kv.SecretV2(
    f"grafana-vault-secrets-{stack_info.env_suffix}",
    name="grafana",
    mount=global_vault_mount,
    data_json=json.dumps(grafana_vault_secrets),
)
