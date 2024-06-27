import json
from pathlib import Path

import pulumi_vault as vault
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider
from pulumi import Config, ResourceOptions

stack_info = parse_stack()
global_vault_secrets = read_yaml_secrets(
    Path(f"vault/secrets.{stack_info.env_suffix}.yaml")
)
if Config("vault_server").get("env_namespace"):
    setup_vault_provider()

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
