import pulumi_vault as vault
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider
from pulumi import ResourceOptions, export

setup_vault_provider()
stack_info = parse_stack()


superset_vault_kv_mount = vault.Mount(
    "superset-vault-kv-secrets-mount",
    path="secret-superset",
    description=("Static secrets storage for Superset"),
    type="kv-v2",
    options={
        "version": 2,
    },
    opts=ResourceOptions(delete_before_replace=True),
)

celery_monitoring_vault_kv_mount = vault.Mount(
    "celery-monitoring-vault-kv-secrets-mount",
    path="secret-celery-monitoring",
    description=("Static secrets storage for celery-monitoring"),
    type="kv-v2",
    options={
        "version": 2,
    },
    opts=ResourceOptions(delete_before_replace=True),
)

export(
    "superset_kv",
    {
        "path": superset_vault_kv_mount.path,
        "type": superset_vault_kv_mount.type,
    },
)

export(
    "celery_monitoring_kv",
    {
        "path": celery_monitoring_vault_kv_mount.path,
        "type": celery_monitoring_vault_kv_mount.type,
    },
)
