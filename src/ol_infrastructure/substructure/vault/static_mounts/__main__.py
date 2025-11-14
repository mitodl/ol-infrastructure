"""Vault static KV mounts for application secrets.

This module creates and exports Vault KV v2 mounts for applications
that need to store static secrets in Vault.
"""

import pulumi_vault as vault
from pulumi import ResourceOptions, export

from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

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

xqwatcher_vault_kv_mount = vault.Mount(
    "xqwatcher-vault-kv-secrets-mount",
    path="secret-xqwatcher",
    description=("Static secrets storage for xqwatcher"),
    type="kv-v2",
    options={
        "version": 2,
    },
    opts=ResourceOptions(delete_before_replace=True),
)

digital_credentials_vault_kv_mount = vault.Mount(
    "digital-credentials-vault-kv-secrets-mount",
    path="secret-digital-credentials",
    description=("Static secrets storage for Digital Credentials Consortium services"),
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

export(
    "xqwatcher_kv",
    {
        "path": xqwatcher_vault_kv_mount.path,
        "type": xqwatcher_vault_kv_mount.type,
    },
)

export(
    "digital_credentials_kv",
    {
        "path": digital_credentials_vault_kv_mount.path,
        "type": digital_credentials_vault_kv_mount.type,
    },
)
