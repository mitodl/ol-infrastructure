import pulumi_vault as vault
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider
from pulumi import export

setup_vault_provider()
stack_info = parse_stack()


superset_vault_kv_mount = vault.Mount(
    "superset-vault-kv-secrets-mount",
    path=f"secret-{stack_info.env_prefix}",
    description=("Static secrets storage for Superset"),
    type="kv-v2",
    options={
        "version": 2,
    },
)

export(
    "superset_kv",
    {
        "path": superset_vault_kv_mount.path,
        "type": superset_vault_kv_mount.type,
    },
)
