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

vault.kv.SecretV2(
    f"grafana-vault-secrets-{stack_info.env_suffix}",
    name="grafana",
    mount=global_vault_mount,
    data_json=json.dumps(grafana_vault_secrets),
)
