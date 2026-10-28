# ruff: noqa: E501
"""Kubernetes Secrets for the deployed MITx Online application.

The Vault-backed ones come from the description in vault_secrets.py; the Redis
credentials are a plain Secret built from the cache's address.
"""

import pulumi_kubernetes as kubernetes
from pulumi import ResourceOptions

from bridge.lib.magic_numbers import DEFAULT_REDIS_PORT
from ol_infrastructure.applications.mitxonline.vault_secrets import vault_secrets
from ol_infrastructure.components.aws.cache import OLAmazonCache
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SResources,
    OLVaultK8SSecret,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo
from ol_infrastructure.lib.secret_env import vault_k8s_secrets


def create_mitxonline_k8s_secrets(  # noqa: PLR0913
    stack_info: StackInfo,
    mitxonline_namespace: str,
    k8s_global_labels: dict[str, str],
    vault_k8s_resources: OLVaultK8SResources,
    db_config: OLVaultDatabaseBackend,
    rds_endpoint: str,
    openedx_environment: str,
    redis_password: str,
    redis_cache: OLAmazonCache,
) -> tuple[list[str], list[OLVaultK8SSecret | kubernetes.core.v1.Secret]]:
    """Create all Kubernetes secrets required by the MITx Online application.

    :param stack_info: Information about the current Pulumi stack.
    :param mitxonline_namespace: The Kubernetes namespace for mitxonline resources.
    :param k8s_global_labels: Standard labels to apply to all Kubernetes resources.
    :param vault_k8s_resources: Vault Kubernetes auth backend resources.
    :param db_config: The Vault dynamic PostgreSQL database backend.
    :param rds_endpoint: ``host:port`` of the RDS instance.
    :param openedx_environment: The full Open edX environment name (e.g. "mitxonline-qa").
    :param redis_password: The Redis auth token.
    :param redis_cache: The app's Redis cache.
    :returns: The Secrets' names, in the order pods list them under ``envFrom``,
        and the resources creating them.
    """
    secrets = vault_secrets(
        database_mount=db_config.db_mount.path,
        database_host=rds_endpoint,
        openedx_environment=openedx_environment,
    )
    resource_prefix = f"ol-mitxonline-{stack_info.env_suffix}"
    vaultauth = vault_k8s_resources.auth_name
    dynamic = vault_k8s_secrets(
        [secret for secret in secrets if secret.kind == "dynamic"],
        resource_prefix=resource_prefix,
        namespace=mitxonline_namespace,
        labels=k8s_global_labels,
        vaultauth=vaultauth,
    )

    redis_creds_secret_name = "redis-creds"  # noqa: S105  # pragma: allowlist secret
    redis_creds = kubernetes.core.v1.Secret(
        f"learn-ai-{stack_info.env_suffix}-redis-creds",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=redis_creds_secret_name,
            namespace=mitxonline_namespace,
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

    static = vault_k8s_secrets(
        [secret for secret in secrets if secret.kind == "static"],
        resource_prefix=resource_prefix,
        namespace=mitxonline_namespace,
        labels=k8s_global_labels,
        vaultauth=vaultauth,
    )
    created: list[tuple[str, OLVaultK8SSecret | kubernetes.core.v1.Secret]] = [
        *dynamic,
        (redis_creds_secret_name, redis_creds),
        *static,
    ]
    return [name for name, _ in created], [resource for _, resource in created]
