# ruff: noqa: E501, PLR0913, S106
"""
Manage Kubernetes secrets for the xPro application using Vault.

This module defines functions to create Kubernetes secrets required by the xPro
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
        f"ol-xpro-{stack_info.env_suffix}-{secret_base_name}-static-secret",
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
        f"ol-xpro-{stack_info.env_suffix}-{secret_base_name}-dynamic-secret",
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


def create_xpro_k8s_secrets(
    stack_info: StackInfo,
    xpro_namespace: str,
    k8s_global_labels: dict[str, str],
    vault_k8s_resources: OLVaultK8SResources,
    db_config: OLVaultDatabaseBackend,
    rds_endpoint: str,
    redis_password: str,
    redis_cache: OLAmazonCache,
) -> tuple[list[str], list[OLVaultK8SSecret | kubernetes.core.v1.Secret]]:
    """
    Create all Kubernetes secrets required by the xPro application.

    Fetches secrets from various Vault backends (static KV, dynamic AWS, dynamic DB)
    and creates corresponding Kubernetes Secret objects managed by the Vault agent.

    Args:
        stack_info: Information about the current Pulumi stack.
        xpro_namespace: The Kubernetes namespace for xPro resources.
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
    aws_secret_name, aws_secret = _create_dynamic_secret(
        stack_info=stack_info,
        secret_base_name="aws-xpro",  # pragma: allowlist secret
        namespace=xpro_namespace,
        labels=k8s_global_labels,
        mount="aws-mitx",
        path="creds/xpro-app",
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
        secret_base_name="postgres-xpro",  # pragma: allowlist secret
        namespace=xpro_namespace,
        labels=k8s_global_labels,
        mount=db_config.db_mount.path,
        path="creds/app",
        templates={
            "DATABASE_URL": f'postgres://{{{{ get .Secrets "username" }}}}:{{{{ get .Secrets "password" }}}}@{rds_endpoint}/xpro'
        },
        vaultauth=vaultauth,
    )
    secret_names.append(db_secret_name)
    secret_resources.append(db_secret)

    # 3. Redis credentials (plain K8s secret)
    redis_creds_secret_name = "redis-creds"  # noqa: S105  # pragma: allowlist secret
    redis_creds = kubernetes.core.v1.Secret(
        f"xpro-{stack_info.env_suffix}-redis-creds",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=redis_creds_secret_name,
            namespace=xpro_namespace,
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

    # 4. Static secrets from 'secret-xpro' KV-v2 mount (per-path secrets)
    xpro_secrets_configs: list[dict[str, Any]] = [
        {
            "base_name": "cybersource",
            "path": "cybersource",
            "templates": {
                "CYBERSOURCE_ACCESS_KEY": '{{ get .Secrets "access_key" }}',
                "CYBERSOURCE_INQUIRY_LOG_NACL_ENCRYPTION_KEY": '{{ get .Secrets "inquiry_log_nacl_encryption_key" }}',
                "CYBERSOURCE_PROFILE_ID": '{{ get .Secrets "profile_id" }}',
                "CYBERSOURCE_SECURITY_KEY": '{{ get .Secrets "security_key" }}',
                "CYBERSOURCE_TRANSACTION_KEY": '{{ get .Secrets "transaction_key" }}',
            },
        },
        {
            "base_name": "digital-credentials",
            "path": "digital-credentials",
            "templates": {
                "DIGITAL_CREDENTIALS_ISSUER_ID": '{{ get .Secrets "issuer_id" }}',
                "DIGITAL_CREDENTIALS_OAUTH2_CLIENT_ID": '{{ get .Secrets "oauth2_client_id" }}',
                "DIGITAL_CREDENTIALS_VERIFICATION_METHOD": '{{ get .Secrets "verification_method" }}',
                "MITOL_DIGITAL_CREDENTIALS_HMAC_SECRET": '{{ get .Secrets "hmac_secret" }}',
                "MITOL_DIGITAL_CREDENTIALS_VERIFY_SERVICE_BASE_URL": '{{ get .Secrets "sign_and_verify_url" }}',
            },
        },
        {
            "base_name": "django",
            "path": "django",
            "templates": {
                "SECRET_KEY": '{{ get .Secrets "secret-key" }}',
                "STATUS_TOKEN": '{{ get .Secrets "status-token" }}',
            },
        },
        {
            "base_name": "emeritus",
            "path": "emeritus",
            "templates": {
                "EMERITUS_API_KEY": '{{ get .Secrets "api_key" }}',
            },
        },
        {
            "base_name": "external-course-sync",
            "path": "external-course-sync",
            "templates": {
                "EXTERNAL_COURSE_SYNC_API_KEY": '{{ get .Secrets "api_key" }}',
                "EXTERNAL_COURSE_SYNC_EMAIL_RECIPIENTS": '{{ get .Secrets "email-recipients" }}',
            },
        },
        {
            "base_name": "google-sheets",
            "path": "google-sheets",
            "templates": {
                "COUPON_REQUEST_SHEET_ID": '{{ get .Secrets "sheet_id" }}',
                "DEFERRAL_REQUEST_WORKSHEET_ID": '{{ get .Secrets "deferral_worksheet_id" }}',
                "DRIVE_OUTPUT_FOLDER_ID": '{{ get .Secrets "folder_id" }}',
                "DRIVE_SERVICE_ACCOUNT_CREDS": '{{ get .Secrets "service_account_creds" }}',
                "DRIVE_SHARED_ID": '{{ get .Secrets "drive_shared_id" }}',
                "ENROLLMENT_CHANGE_SHEET_ID": '{{ get .Secrets "enroll_change_sheet_id" }}',
                "REFUND_REQUEST_WORKSHEET_ID": '{{ get .Secrets "refund_worksheet_id" }}',
                "SHEETS_ADMIN_EMAILS": '{{ get .Secrets "admin_emails" }}',
            },
        },
        {
            "base_name": "hirefire",
            "path": "hirefire",
            "templates": {
                "HIREFIRE_TOKEN": '{{ get .Secrets "token" }}',
            },
        },
        {
            "base_name": "hubspot",
            "path": "hubspot",
            "templates": {
                "MITOL_HUBSPOT_API_PRIVATE_TOKEN": '{{ get .Secrets "api_private_token" }}',
            },
        },
        {
            "base_name": "openedx",
            "path": "openedx",
            "templates": {
                "MITXPRO_REGISTRATION_ACCESS_TOKEN": '{{ get .Secrets "registration_access_token" }}',
                "OPENEDX_GRADES_API_TOKEN": '{{ get .Secrets "grades_api_token" }}',
                "OPENEDX_SERVICE_WORKER_API_TOKEN": '{{ get .Secrets "service_worker_api_token" }}',
            },
        },
        {
            "base_name": "openedx-api-client",
            "path": "openedx-api-client",
            "templates": {
                "OPENEDX_API_CLIENT_ID": '{{ get .Secrets "client_id" }}',
                "OPENEDX_API_CLIENT_SECRET": '{{ get .Secrets "client_secret" }}',
            },
        },
        {
            "base_name": "posthog",
            "path": "posthog",
            "templates": {
                "POSTHOG_PROJECT_API_KEY": '{{ get .Secrets "project_api_key" }}',
            },
        },
        {
            "base_name": "recaptcha",
            "path": "recaptcha",
            "templates": {
                "RECAPTCHA_SECRET_KEY": '{{ get .Secrets "secret_key" }}',
                "RECAPTCHA_SITE_KEY": '{{ get .Secrets "site_key" }}',
            },
        },
        {
            "base_name": "sentry",
            "path": "sentry",
            "templates": {
                "SENTRY_DSN": '{{ get .Secrets "dsn" }}',
            },
        },
        {
            "base_name": "smtp",
            "path": "smtp",
            "templates": {
                "MITXPRO_EMAIL_HOST": '{{ get .Secrets "relay_host" }}',
                "MITXPRO_EMAIL_PASSWORD": '{{ get .Secrets "relay_password" }}',
                "MITXPRO_EMAIL_PORT": '{{ get .Secrets "relay_port" }}',
                "MITXPRO_EMAIL_USER": '{{ get .Secrets "relay_username" }}',
                "MITXPRO_SUPPORT_EMAIL": '{{ get .Secrets "support_email" }}',
            },
        },
        {
            "base_name": "voucher-domestic",
            "path": "voucher-domestic",
            "templates": {
                "VOUCHER_DOMESTIC_AMOUNT_KEY": '{{ get .Secrets "amount_key" }}',
                "VOUCHER_DOMESTIC_COURSE_KEY": '{{ get .Secrets "course_key" }}',
                "VOUCHER_DOMESTIC_CREDITS_KEY": '{{ get .Secrets "credits_key" }}',
                "VOUCHER_DOMESTIC_DATES_KEY": '{{ get .Secrets "dates_key" }}',
                "VOUCHER_DOMESTIC_DATE_KEY": '{{ get .Secrets "date_key" }}',
                "VOUCHER_DOMESTIC_EMPLOYEE_ID_KEY": '{{ get .Secrets "employee_id_key" }}',
                "VOUCHER_DOMESTIC_EMPLOYEE_KEY": '{{ get .Secrets "employee_key" }}',
                "VOUCHER_DOMESTIC_KEY": '{{ get .Secrets "key" }}',
            },
        },
    ]

    for config in xpro_secrets_configs:
        secret_name, secret_resource = _create_static_secret(
            stack_info=stack_info,
            secret_base_name=f"xpro-{config['base_name']}",
            namespace=xpro_namespace,
            labels=k8s_global_labels,
            mount="secret-xpro",
            mount_type="kv-v1",
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
            namespace=xpro_namespace,
            labels=k8s_global_labels,
            mount="secret-global",
            mount_type="kv-v2",
            path=config["path"],
            templates=config["templates"],
            vaultauth=vaultauth,
        )
        secret_names.append(secret_name)
        secret_resources.append(secret_resource)

    return secret_names, secret_resources
