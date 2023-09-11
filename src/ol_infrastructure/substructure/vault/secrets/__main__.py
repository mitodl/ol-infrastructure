import json
from pathlib import Path

import pulumi_vault as vault
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib.pulumi_helper import parse_stack
from pulumi import ResourceOptions

stack_info = parse_stack()
global_vault_secrets = read_yaml_secrets(
    Path(f"vault/secrets.{stack_info.env_suffix}.yaml")
)

# Create the secret mount used for storing global secrets
global_vault_mount = vault.Mount(
    "global-secrets-mount",
    path="secret-global",
    type="kv-v2",
    options={"version": 2},
    description="Storage of global secrets used across our applications.",
    opts=ResourceOptions(delete_before_replace=True),
)

vault.generic.Secret(
    "global-vault-secrets",
    path=global_vault_mount.path.apply("{}/data".format),
    data_json=json.dumps(global_vault_secrets),
)
