# ruff: noqa: E501, PLR0913
"""Kubernetes Secrets for the deployed MIT Learn application.

The Vault-backed ones come from the description in vault_secrets.py; the Redis
credentials are a plain Secret built from the cache's address.
"""

import pulumi_kubernetes as kubernetes
from pulumi import ResourceOptions
from pulumi_vault import Mount

from bridge.lib.magic_numbers import DEFAULT_REDIS_PORT
from ol_infrastructure.applications.mit_learn.vault_secrets import (
    MEMORY_SECRET,
    OPIK_SECRET,
    STARROCKS_SECRET,
    vault_secrets,
)
from ol_infrastructure.components.aws.cache import OLAmazonCache
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SResources,
    OLVaultK8SSecret,
    OLVaultRestartTarget,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo
from ol_infrastructure.lib.secret_env import vault_k8s_secrets


def create_mitlearn_k8s_secrets(
    stack_info: StackInfo,
    mitlearn_namespace: str,
    k8s_global_labels: dict[str, str],
    vault_k8s_resources: OLVaultK8SResources,
    mitlearn_vault_mount: Mount,
    db_config: OLVaultDatabaseBackend,
    redis_password: str,
    redis_cache: OLAmazonCache,
    restart_targets: list[OLVaultRestartTarget],
    starrocks_vault_mount_path: str | None = None,
    *,
    memory_enabled: bool = False,
) -> tuple[list[str], list[str], list[OLVaultK8SSecret | kubernetes.core.v1.Secret]]:
    """Create all Kubernetes secrets required by the mitlearn application.

    :param stack_info: Information about the current Pulumi stack.
    :param mitlearn_namespace: The Kubernetes namespace for mitlearn resources.
    :param k8s_global_labels: Standard labels to apply to all Kubernetes resources.
    :param vault_k8s_resources: Vault Kubernetes auth backend resources.
    :param mitlearn_vault_mount: The Vault mount resource for mitlearn secrets.
    :param db_config: The Vault dynamic PostgreSQL database backend.
    :param redis_password: The Redis auth token.
    :param redis_cache: The app's Redis cache.
    :param restart_targets: Every Deployment that reads these secrets through
        envFrom, rolled when the StarRocks lease rotates or the Opik or memory
        Keycloak client changes. Without these the new value lands in the
        Secret while running pods keep the old one.
    :param starrocks_vault_mount_path: Vault mount serving dynamic StarRocks
        credentials, or None to skip them. None for any stack that has no
        STARROCKS_HOST configured; see the caller.
    :param memory_enabled: Deliver the memory token-exchange client, in the
        environments that run the memory service.
    :returns: The names of the Secrets pods require, in the order pods list
        them under ``envFrom``; the names of those pods mount as optional (the
        Opik client, which another stack writes to Vault, so pods still start
        if that stack hasn't applied yet; VSO doesn't restart the targets on a
        secret's first sync, so pods that started without it trace
        unauthenticated until something else rolls them); and the resources
        creating them all.
    """
    secrets = vault_secrets(
        mitlearn_mount=mitlearn_vault_mount.path,
        database_mount=db_config.db_mount.path,
        database_host=f"ol-mitlearn-db-{stack_info.name.lower()}.cbnm7ajau6mi.us-east-1.rds.amazonaws.com:5432",
        starrocks_mount=starrocks_vault_mount_path,
        memory_enabled=memory_enabled,
    )
    vso = vault_k8s_secrets(
        secrets,
        resource_prefix=f"ol-mitlearn-{stack_info.env_suffix}",
        namespace=mitlearn_namespace,
        labels=k8s_global_labels,
        vaultauth=vault_k8s_resources.auth_name,
        restart_targets={
            secret.name: restart_targets
            for secret in secrets
            if secret.name in {OPIK_SECRET, MEMORY_SECRET, STARROCKS_SECRET}
        },
    )

    redis_creds_secret_name = "redis-creds"  # noqa: S105  # pragma: allowlist secret
    redis_creds = kubernetes.core.v1.Secret(
        f"learn-ai-{stack_info.env_suffix}-redis-creds",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=redis_creds_secret_name,
            namespace=mitlearn_namespace,
            labels=k8s_global_labels,
        ),
        string_data=redis_cache.address.apply(
            lambda address: {
                "REDIS_URL": f"rediss://default:{redis_password}@{address}:{DEFAULT_REDIS_PORT}",  # Value in heroku omits db
                "CELERY_BROKER_URL": f"rediss://default:{redis_password}@{address}:{DEFAULT_REDIS_PORT}/1?ssl_cert_reqs=required",
                "CELERY_RESULT_BACKEND": f"rediss://default:{redis_password}@{address}:{DEFAULT_REDIS_PORT}/1?ssl_cert_reqs=required",
            }
        ),
        opts=ResourceOptions(
            depends_on=[redis_cache],
            delete_before_replace=True,
        ),
    )

    created: list[tuple[str, OLVaultK8SSecret | kubernetes.core.v1.Secret]] = [
        *vso,
        (redis_creds_secret_name, redis_creds),
    ]
    optional = {secret.secret_name for secret in secrets if secret.name == OPIK_SECRET}
    return (
        [name for name, _ in created if name not in optional],
        [name for name, _ in created if name in optional],
        [resource for _, resource in created],
    )
