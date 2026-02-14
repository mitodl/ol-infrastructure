# ruff: noqa: E501, PLR0913, S106
"""
Manage Kubernetes secrets for the OCW Studio application using Vault.

This module defines functions to create Kubernetes secrets required by the OCW Studio
application by fetching data from various Vault secret backends (static KV and dynamic).
"""

from typing import Any

import pulumi_kubernetes as kubernetes
from pulumi import ResourceOptions

from bridge.lib.magic_numbers import DEFAULT_REDIS_PORT
from ol_infrastructure.components.aws.cache import OLAmazonCache
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SResources,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo


def _create_static_secret(
    stack_info: StackInfo,
    secret_base_name: str,
    namespace: str,
    labels: dict[str, str],
    mount: str,
    path: str,
    templates: dict[str, str],
    vaultauth: str,
    mount_type: str = "kv-v1",
    opts: ResourceOptions | None = None,
) -> tuple[str, OLVaultK8SSecret]:
    """
    Create an OLVaultK8SSecret resource for a static Vault secret.

    Args:
        stack_info: Information about the current Pulumi stack.
        secret_base_name: Base name for the Kubernetes secret resource.
        namespace: Kubernetes namespace where the secret will be created.
        labels: Labels to apply to the Kubernetes secret.
        mount: Vault mount path for the secret backend.
        path: Path within the Vault mount where the secret data resides.
        templates: Dictionary defining how Vault data maps to Kubernetes secret keys.
        vaultauth: Name of the Vault Kubernetes auth backend role.
        mount_type: Type of the Vault mount (e.g., "kv-v1", "kv-v2"). Defaults to "kv-v1".
        opts: Optional Pulumi resource options.

    Returns:
        A tuple containing the generated Kubernetes secret name and the resource object.
    """
    secret_name = f"{secret_base_name}-static-secret"
    resource = OLVaultK8SSecret(
        f"ol-ocw-studio-{stack_info.env_suffix}-{secret_base_name}-static-secret",
        resource_config=OLVaultK8SStaticSecretConfig(
            name=secret_name,
            namespace=namespace,
            labels=labels,
            dest_secret_name=secret_name,
            dest_secret_labels=labels,
            mount=mount,
            mount_type=mount_type,
            path=path,
            excludes=[".*"],
            exclude_raw=True,
            templates=templates,
            vaultauth=vaultauth,
        ),
        opts=opts,
    )
    return secret_name, resource


def _create_dynamic_secret(
    stack_info: StackInfo,
    secret_base_name: str,
    namespace: str,
    labels: dict[str, str],
    mount: str,
    path: str,
    templates: dict[str, str],
    vaultauth: str,
    opts: ResourceOptions | None = None,
) -> tuple[str, OLVaultK8SSecret]:
    """
    Create an OLVaultK8SSecret resource for a dynamic Vault secret.

    Args:
        stack_info: Information about the current Pulumi stack.
        secret_base_name: Base name for the Kubernetes secret resource.
        namespace: Kubernetes namespace where the secret will be created.
        labels: Labels to apply to the Kubernetes secret.
        mount: Vault mount path for the secret backend (e.g., database, aws).
        path: Path within the Vault mount to generate credentials (e.g., creds/role).
        templates: Dictionary defining how Vault data maps to Kubernetes secret keys.
        vaultauth: Name of the Vault Kubernetes auth backend role.
        opts: Optional Pulumi resource options.

    Returns:
        A tuple containing the generated Kubernetes secret name and the resource object.
    """
    secret_name = f"{secret_base_name}-dynamic-secret"
    resource = OLVaultK8SSecret(
        f"ol-ocw-studio-{stack_info.env_suffix}-{secret_base_name}-dynamic-secret",
        resource_config=OLVaultK8SDynamicSecretConfig(
            name=secret_name,
            namespace=namespace,
            labels=labels,
            dest_secret_name=secret_name,
            dest_secret_labels=labels,
            mount=mount,
            path=path,
            excludes=[".*"],
            exclude_raw=True,
            templates=templates,
            vaultauth=vaultauth,
        ),
        opts=opts,
    )
    return secret_name, resource


def create_ocw_studio_k8s_secrets(
    stack_info: StackInfo,
    ocw_studio_namespace: str,
    k8s_global_labels: dict[str, str],
    vault_k8s_resources: OLVaultK8SResources,
    db_config: OLVaultDatabaseBackend,
    rds_endpoint: str,
    redis_password: str,
    redis_cache: OLAmazonCache,
) -> tuple[list[str], list[OLVaultK8SSecret | kubernetes.core.v1.Secret]]:
    """
    Create all Kubernetes secrets required by the OCW Studio application.

    Fetches secrets from various Vault backends (static KV, dynamic AWS, dynamic DB)
    and creates corresponding Kubernetes Secret objects managed by the Vault agent.

    Args:
        stack_info: Information about the current Pulumi stack.
        ocw_studio_namespace: The Kubernetes namespace for OCW Studio resources.
        k8s_global_labels: Standard labels to apply to all Kubernetes resources.
        vault_k8s_resources: Vault Kubernetes auth backend resources.
        db_config: Configuration for the Vault dynamic PostgreSQL database backend.
        rds_endpoint: The endpoint address of the RDS instance.
        redis_password: The password for the Redis cluster.
        redis_cache: The Redis cache resource for connection details.

    Returns:
        A tuple containing a list of the names of the created Kubernetes secrets
        and a list of the corresponding Pulumi resource objects.
    """
    secret_names: list[str] = []
    secret_resources: list[OLVaultK8SSecret | kubernetes.core.v1.Secret] = []

    vaultauth = vault_k8s_resources.auth_name

    # 1. Dynamic AWS credentials
    # App does not support IAM roles for service accounts (IRSA)
    # so we use dynamic AWS credentials from Vault instead
    aws_secret_name, aws_secret = _create_dynamic_secret(
        stack_info=stack_info,
        secret_base_name="aws-ocw-studio",  # pragma: allowlist secret
        namespace=ocw_studio_namespace,
        labels=k8s_global_labels,
        mount="aws-mitx",
        path=f"creds/ocw-studio-app-{stack_info.env_suffix}",
        templates={
            "AWS_ACCESS_KEY_ID": '{{ get .Secrets "access_key" }}',
            "AWS_SECRET_ACCESS_KEY": '{{ get .Secrets "secret_key" }}',
        },
        vaultauth=vaultauth,
    )
    secret_names.append(aws_secret_name)
    secret_resources.append(aws_secret)

    # 2. Dynamic PostgreSQL credentials
    db_secret_name, db_secret = _create_dynamic_secret(
        stack_info=stack_info,
        secret_base_name="postgres-ocw-studio",  # pragma: allowlist secret
        namespace=ocw_studio_namespace,
        labels=k8s_global_labels,
        mount=db_config.db_mount.path,
        path="creds/app",
        templates={
            "DATABASE_URL": f'postgres://{{{{ get .Secrets "username" }}}}:{{{{ get .Secrets "password" }}}}@{rds_endpoint}/ocw_studio'
        },
        vaultauth=vaultauth,
    )
    secret_names.append(db_secret_name)
    secret_resources.append(db_secret)

    # 3. Redis credentials (plain K8s secret)
    redis_creds_secret_name = "redis-creds"  # noqa: S105  # pragma: allowlist secret
    redis_creds = kubernetes.core.v1.Secret(
        f"ocw-studio-{stack_info.env_suffix}-redis-creds",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=redis_creds_secret_name,
            namespace=ocw_studio_namespace,
            labels=k8s_global_labels,
        ),
        string_data=redis_cache.address.apply(
            lambda address: {
                "REDIS_URL": f"rediss://default:{redis_password}@{address}:{DEFAULT_REDIS_PORT}",
                "CELERY_BROKER_URL": f"rediss://default:{redis_password}@{address}:{DEFAULT_REDIS_PORT}/1?ssl_cert_reqs=required",
                "CELERY_RESULT_BACKEND": f"rediss://default:{redis_password}@{address}:{DEFAULT_REDIS_PORT}/1?ssl_cert_reqs=required",
            }
        ),
        opts=ResourceOptions(
            depends_on=[redis_cache],
            delete_before_replace=True,
        ),
    )
    secret_names.append(redis_creds_secret_name)
    secret_resources.append(redis_creds)

    # 4. Static secrets from 'secret-ocw-studio' KV-v2 mount (collected secrets)
    ocw_studio_secrets_configs: list[dict[str, Any]] = [
        {
            "base_name": "collected",
            "path": "collected",
            "templates": {
                "DRIVE_SERVICE_ACCOUNT_CREDS": '{{ index .Secrets "google" "drive_service_json" }}',
                "GIT_TOKEN": '{{ index .Secrets "github" "user_token" }}',
                "GITHUB_APP_PRIVATE_KEY": '{{ index .Secrets "github" "app_private_key" }}',
                "GITHUB_WEBHOOK_KEY": '{{ index .Secrets "github" "shared_secret" }}',
                "OPEN_CATALOG_WEBHOOK_KEY": '{{ get .Secrets "open_catalog_webhook_key" }}',
                "SECRET_KEY": '{{ index .Secrets "django" "secret_key" }}',
                "SENTRY_DSN": '{{ get .Secrets "sentry_dsn" }}',
                "SOCIAL_AUTH_SAML_IDP_X509": '{{ index .Secrets "saml" "idp_x509" }}',
                "SOCIAL_AUTH_SAML_SP_PRIVATE_KEY": '{{ index .Secrets "saml" "sp_private_key" }}',
                "SOCIAL_AUTH_SAML_SP_PUBLIC_CERT": '{{ index .Secrets "saml" "sp_public_cert" }}',
                "STATUS_TOKEN": '{{ index .Secrets "django" "status_token" }}',
                "THREEPLAY_API_KEY": '{{ index .Secrets "threeplay" "api_key" }}',
                "THREEPLAY_CALLBACK_KEY": '{{ index .Secrets "threeplay" "callback_key" }}',
                "VIDEO_S3_TRANSCODE_ENDPOINT": '{{ get .Secrets "transcode_endpoint" }}',
                "YT_ACCESS_TOKEN": '{{ index .Secrets "youtube" "access_token" }}',
                "YT_CLIENT_ID": '{{ index .Secrets "youtube" "client_id" }}',
                "YT_CLIENT_SECRET": '{{ index .Secrets "youtube" "client_secret" }}',
                "YT_REFRESH_TOKEN": '{{ index .Secrets "youtube" "refresh_token" }}',
            },
        },
    ]

    for config in ocw_studio_secrets_configs:
        secret_name, secret_resource = _create_static_secret(
            stack_info=stack_info,
            secret_base_name=f"ocw-studio-{config['base_name']}",
            namespace=ocw_studio_namespace,
            labels=k8s_global_labels,
            mount="secret-ocw-studio",
            mount_type="kv-v2",
            path=config["path"],
            templates=config["templates"],
            vaultauth=vaultauth,
        )
        secret_names.append(secret_name)
        secret_resources.append(secret_resource)

    # 5. Static secrets from 'secret-global' KV-v2 mount (mailgun)
    global_secrets_configs: list[dict[str, Any]] = [
        {
            "base_name": "mailgun",
            "path": "mailgun",
            "templates": {
                "MAILGUN_KEY": '{{ get .Secrets "api_key" }}',
            },
        },
    ]
    for config in global_secrets_configs:
        secret_name, secret_resource = _create_static_secret(
            stack_info=stack_info,
            secret_base_name=f"global-{config['base_name']}",
            namespace=ocw_studio_namespace,
            labels=k8s_global_labels,
            mount="secret-global",
            mount_type="kv-v2",
            path=config["path"],
            templates=config["templates"],
            vaultauth=vaultauth,
        )
        secret_names.append(secret_name)
        secret_resources.append(secret_resource)

    # 6. Static secrets from 'secret-concourse' KV-v1 mount
    concourse_secrets_configs: list[dict[str, Any]] = [
        {
            "base_name": "concourse-api-bearer-token",
            "mount": "secret-concourse",
            "path": "ocw/api-bearer-token",
            "templates": {
                "API_BEARER_TOKEN": '{{ get .Secrets "value" }}',
            },
        },
        {
            "base_name": "concourse-web",
            "mount": "secret-concourse",
            "path": "web",
            "templates": {
                "CONCOURSE_PASSWORD": '{{ get .Secrets "admin_password" }}',
            },
        },
    ]
    for config in concourse_secrets_configs:
        secret_name, secret_resource = _create_static_secret(
            stack_info=stack_info,
            secret_base_name=f"ocw-studio-{config['base_name']}",
            namespace=ocw_studio_namespace,
            labels=k8s_global_labels,
            mount=config["mount"],
            path=config["path"],
            templates=config["templates"],
            vaultauth=vaultauth,
        )
        secret_names.append(secret_name)
        secret_resources.append(secret_resource)

    return secret_names, secret_resources
