import json
from functools import partial
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

for key, data in global_vault_secrets.items():
    secret_path = partial("{1}/{0}".format, key)
    vault.generic.Secret(
        f"global-vault-secrets-{key}",
        path=global_vault_mount.path.apply(secret_path),
        data_json=json.dumps(data),
    )
