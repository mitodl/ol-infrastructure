# ruff: noqa: E501, PLR0913, S106
"""
Manage Kubernetes secrets for the MITx Online application using Vault.

This module defines functions to create Kubernetes secrets required by the MITx Online
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
        f"ol-mitxonline-{stack_info.env_suffix}-{secret_base_name}-static-secret",  # Pulumi resource name
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
        f"ol-mitxonline-{stack_info.env_suffix}-{secret_base_name}-dynamic-secret",  # Pulumi resource name
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


def create_mitxonline_k8s_secrets(
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
    """
    Create all Kubernetes secrets required by the MITx Online application.

    Fetches secrets from various Vault backends (static KV, dynamic AWS, dynamic DB)
    and creates corresponding Kubernetes Secret objects managed by the Vault agent.

    Args:
        stack_info: Information about the current Pulumi stack.
        mitxonline_namespace: The Kubernetes namespace for mitxonline resources.
        k8s_global_labels: Standard labels to apply to all Kubernetes resources.
        vault_k8s_resources: Vault Kubernetes auth backend resources.
        db_config: Configuration for the Vault dynamic PostgreSQL database backend.
        rds_endpoint: The endpoint address of the RDS instance.
        openedx_environment: The full Open edX environment name (e.g., "mitxonline-qa").

    Returns:
        A tuple containing a list of the names of the created Kubernetes secrets
        and a list of the corresponding Pulumi resource objects.
    """
    secret_names: list[str] = []
    secret_resources: list[
        OLVaultK8SSecret | kubernetes.core.v1.Secret
    ] = []  # Keep track of resources if needed later

    vaultauth = vault_k8s_resources.auth_name

    # 1. Dynamic AWS credentials
    aws_secret_name, aws_secret = _create_dynamic_secret(
        stack_info=stack_info,
        secret_base_name="aws-mitxonline",  # pragma: allowlist secret
        namespace=mitxonline_namespace,
        labels=k8s_global_labels,
        mount="aws-mitx",
        path="creds/mitxonline",
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
        secret_base_name="postgres-mitxonline",  # pragma: allowlist secret
        namespace=mitxonline_namespace,
        labels=k8s_global_labels,
        mount=db_config.db_mount.path,
        path="creds/app",
        templates={
            "DATABASE_URL": f'postgres://{{{{ get .Secrets "username" }}}}:{{{{ get .Secrets "password" }}}}@{rds_endpoint}/mitxonline'
        },
        vaultauth=vaultauth,
    )
    secret_names.append(db_secret_name)
    secret_resources.append(db_secret)

    # 2.5 A regular k8s secret for redis credentials
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
    secret_names.append(redis_creds_secret_name)
    secret_resources.append(
        redis_creds
    )  # This is different from everything else in the list but allowed per the type hints above

    # 3. Static secrets from 'secret-mitxonline' KV-v1 mount
    mitxonline_secrets_configs: list[dict[str, Any]] = [
        {
            "base_name": "collected",
            "path": "collected-static-secrets",
            "templates": {
                "HUBSPOT_HOME_PAGE_FORM_GUID": '{{ index .Secrets "hubspot" "formId" }}',
                "HUBSPOT_PORTAL_ID": '{{ index .Secrets "hubspot" "portalId" }}',
                "MITOL_GOOGLE_SHEETS_DRIVE_API_PROJECT_ID": '{{ index .Secrets "google-sheets" "drive-api-project-id" }}',
                "MITOL_GOOGLE_SHEETS_DRIVE_CLIENT_ID": '{{ index .Secrets "google-sheets" "drive-client-id" }}',
                "MITOL_GOOGLE_SHEETS_DRIVE_CLIENT_SECRET": '{{ index .Secrets "google-sheets" "drive-client-secret" }}',
                "MITOL_GOOGLE_SHEETS_ENROLLMENT_CHANGE_SHEET_ID": '{{ index .Secrets "google-sheets" "enrollment-change-sheet-id" }}',
                "MITOL_HUBSPOT_API_PRIVATE_TOKEN": '{{ index .Secrets "hubspot" "private-api-token" }}',
                "MITOL_PAYMENT_GATEWAY_CYBERSOURCE_ACCESS_KEY": '{{ index .Secrets "cybersource" "access-key" }}',
                "MITOL_PAYMENT_GATEWAY_CYBERSOURCE_MERCHANT_ID": '{{ index .Secrets "cybersource" "merchant-id" }}',
                "MITOL_PAYMENT_GATEWAY_CYBERSOURCE_MERCHANT_SECRET": '{{ index .Secrets "cybersource" "merchant-secret" }}',
                "MITOL_PAYMENT_GATEWAY_CYBERSOURCE_MERCHANT_SECRET_KEY_ID": '{{ index .Secrets "cybersource" "merchant-secret-key-id" }}',
                "MITOL_PAYMENT_GATEWAY_CYBERSOURCE_PROFILE_ID": '{{ index .Secrets "cybersource" "profile-id" }}',
                "MITOL_PAYMENT_GATEWAY_CYBERSOURCE_SECURITY_KEY": '{{ index .Secrets "cybersource" "security-key" }}',
                "MITX_ONLINE_REFINE_OIDC_CONFIG_CLIENT_ID": '{{ index .Secrets "refine-oidc" "client-id" }}',
                "OIDC_RSA_PRIVATE_KEY": '{{ index .Secrets "refine-oidc" "rsa-private-key" }}',
                "OPENEDX_API_CLIENT_ID": '{{ index .Secrets "open-edx-api-client" "client-id" }}',
                "OPENEDX_API_CLIENT_SECRET": '{{ index .Secrets "open-edx-api-client" "client-secret" }}',
                "OPENEDX_COURSES_SERVICE_WORKER_CLIENT_ID": '{{ index .Secrets "open-edx-courses-service-worker" "client-id" }}',
                "OPENEDX_COURSES_SERVICE_WORKER_CLIENT_SECRET": '{{ index .Secrets "open-edx-courses-service-worker" "client-secret" }}',
                "OPENEDX_RETIREMENT_SERVICE_WORKER_CLIENT_ID": '{{ index .Secrets "open-edx-retirement-service-worker" "client-id" }}',
                "OPENEDX_RETIREMENT_SERVICE_WORKER_CLIENT_SECRET": '{{ index .Secrets "open-edx-retirement-service-worker" "client-secret" }}',
                "OPENEDX_SERVICE_WORKER_API_TOKEN": '{{ index .Secrets "open-edx-service-worker" "api-token" }}',
                "OPEN_EXCHANGE_RATES_APP_ID": '{{ index .Secrets "open-exchange-rates" "app-id" }}',
                "POSTHOG_API_TOKEN": '{{ index .Secrets "posthog" "api-token" }}',
                "POSTHOG_PROJECT_API_KEY": '{{ index .Secrets "posthog" "api-token" }}',
                "RECAPTCHA_SECRET_KEY": '{{ index .Secrets "recaptcha" "secret-key" }}',
                "RECAPTCHA_SITE_KEY": '{{ index .Secrets "recaptcha" "site-key" }}',
                "SECRET_KEY": '{{ index .Secrets "django" "secret-key" }}',
                "STATUS_TOKEN": '{{ index .Secrets "django" "status-token" }}',
                "TRINO_CATALOG": "ol_data_lake_production",
                "TRINO_HOST": "mitol-ol-data-interactive.trino.galaxy.starburst.io",
                "TRINO_PASSWORD": '{{ index .Secrets "data_platform" "trino_password" }}',
                "TRINO_PORT": "443",
                "TRINO_USER": '{{ index .Secrets "data_platform" "trino_username" }}',
            },
        },
        {
            "base_name": "mitxonline-registration-access-token",
            "path": f"{openedx_environment}/mitxonline-registration-access-token",
            "templates": {
                "MITX_ONLINE_REGISTRATION_ACCESS_TOKEN": '{{ get .Secrets "value" }}',
            },
        },
        {
            "base_name": "keycloak-scim-details",
            "path": "keycloak-scim",
            "templates": {
                "MITOL_SCIM_KEYCLOAK_CLIENT_ID": '{{ get .Secrets "client_id" }}',
                "MITOL_SCIM_KEYCLOAK_CLIENT_SECRET": '{{ get .Secrets "client_secret" }}',
            },
        },
    ]

    for config in mitxonline_secrets_configs:
        secret_name, secret_resource = _create_static_secret(
            stack_info=stack_info,
            secret_base_name=f"mitxonline-{config['base_name']}",
            namespace=mitxonline_namespace,
            labels=k8s_global_labels,
            mount="secret-mitxonline",
            path=config["path"],
            templates=config["templates"],
            vaultauth=vaultauth,
        )
        secret_names.append(secret_name)
        secret_resources.append(secret_resource)

    # 4. Static secrets from 'secret-global' KV-v1 mount
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
            namespace=mitxonline_namespace,
            labels=k8s_global_labels,
            mount="secret-global",
            mount_type="kv-v2",
            path=config["path"],
            templates=config["templates"],
            vaultauth=vaultauth,
        )
        secret_names.append(secret_name)
        secret_resources.append(secret_resource)

    # 5. Static secrets from 'secret-operations' KV-v1 mount
    operations_secrets_configs: list[dict[str, Any]] = [
        {
            "base_name": "mitxonline-sentry-dsn",
            "path": "global/mitxonline/sentry-dsn",
            "templates": {
                "SENTRY_DSN": '{{ get .Secrets "value" }}',
            },
        },
    ]
    for config in operations_secrets_configs:
        secret_name, secret_resource = _create_static_secret(
            stack_info=stack_info,
            secret_base_name=f"operations-{config['base_name']}",
            namespace=mitxonline_namespace,
            labels=k8s_global_labels,
            mount="secret-operations",
            path=config["path"],
            templates=config["templates"],
            vaultauth=vaultauth,
        )
        secret_names.append(secret_name)
        secret_resources.append(secret_resource)

    return secret_names, secret_resources
