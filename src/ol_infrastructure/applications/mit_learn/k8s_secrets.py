# ruff: noqa: E501, PLR0913
"""
Manage Kubernetes secrets for the mitlearn application using Vault.

This module defines functions to create Kubernetes secrets required by the mitlearn
application by fetching data from various Vault secret backends (static KV and dynamic).
"""

from typing import Any

import pulumi_kubernetes as kubernetes
from pulumi import ResourceOptions
from pulumi_vault import Mount

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
        f"ol-mitlearn-{stack_info.env_suffix}-{secret_base_name}-static-secret",  # Pulumi resource name
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
        f"ol-mitlearn-{stack_info.env_suffix}-{secret_base_name}-dynamic-secret",  # Pulumi resource name
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


def create_mitlearn_k8s_secrets(
    stack_info: StackInfo,
    mitlearn_namespace: str,
    k8s_global_labels: dict[str, str],
    vault_k8s_resources: OLVaultK8SResources,
    mitlearn_vault_mount: Mount,
    db_config: OLVaultDatabaseBackend,
    redis_password: str,
    redis_cache: OLAmazonCache,
) -> tuple[list[str], list[OLVaultK8SSecret | kubernetes.core.v1.Secret]]:
    """
    Create all Kubernetes secrets required by the mitlearn application.

    Fetches secrets from various Vault backends (static KV, dynamic AWS, dynamic DB)
    and creates corresponding Kubernetes Secret objects managed by the Vault agent.

    Args:
        stack_info: Information about the current Pulumi stack.
        mitlearn_namespace: The Kubernetes namespace for mitlearn resources.
        k8s_global_labels: Standard labels to apply to all Kubernetes resources.
        vault_k8s_resources: Vault Kubernetes auth backend resources.
        mitlearn_vault_mount: The Vault mount resource for mitlearn secrets.
        db_config: Configuration for the Vault dynamic PostgreSQL database backend.

    Returns:
        A list of the names of the created Kubernetes secrets.
    """
    secret_names: list[str] = []
    secret_resources: list[
        OLVaultK8SSecret | kubernetes.core.v1.Secret
    ] = []  # Keep track of resources if needed later

    # 1. Static secrets from the mitlearn KV-v2 mount
    # These secrets are specific to the mitlearn application environment.
    # Static secrets derived from mitopen/secrets.*.yaml, fetched via Vault agent
    mitlearn_static_secret_name, mitlearn_static_secret = _create_static_secret(
        stack_info=stack_info,
        secret_base_name="mitopen",  # Base name for the K8s secret resource  # pragma: allowlist secret  # noqa: S106
        namespace=mitlearn_namespace,
        labels=k8s_global_labels,
        mount=mitlearn_vault_mount.path,
        mount_type="kv-v2",  # This mount is kv-v2
        path="secrets",
        templates={
            # Nested keys require `index .Secrets "parent" "child"` syntax
            "CKEDITOR_ENVIRONMENT_ID": '{{ index .Secrets "ckeditor" "environment_id" }}',
            "CKEDITOR_SECRET_KEY": '{{ index .Secrets "ckeditor" "secret_key" }}',
            "CKEDITOR_UPLOAD_URL": '{{ index .Secrets "ckeditor" "upload_url" }}',
            "EDX_API_CLIENT_ID": '{{ index .Secrets "edx_api_client" "id" }}',
            "EDX_API_CLIENT_SECRET": '{{ index .Secrets "edx_api_client" "secret" }}',
            "OLL_API_CLIENT_ID": '{{ index .Secrets "open_learning_library_client" "client_id" }}',
            "OLL_API_CLIENT_SECRET": '{{ index .Secrets "open_learning_library_client" "client_secret" }}',
            "OPENAI_API_KEY": '{{ index .Secrets "openai" "api_key" }}',
            "OPENSEARCH_HTTP_AUTH": '{{ index .Secrets "opensearch" "http_auth" }}',
            "QDRANT_API_KEY": '{{ index .Secrets "qdrant" "api_key" }}',
            "QDRANT_HOST": '{{ index .Secrets "qdrant" "host_url" }}',
            "POSTHOG_PROJECT_API_KEY": '{{ index .Secrets "posthog" "project_api_key" }}',
            "POSTHOG_PERSONAL_API_KEY": '{{ index .Secrets "posthog" "personal_api_key" }}',
            "SEE_API_CLIENT_ID": '{{ index .Secrets "see_api_client" "id" }}',
            "SEE_API_CLIENT_SECRET": '{{ index .Secrets "see_api_client" "secret" }}',
            # Top-level keys can use `get .Secrets "key"`
            "MITOL_JWT_SECRET": '{{ get .Secrets "jwt_secret" }}',
            "SECRET_KEY": '{{ get .Secrets "django_secret_key" }}',
            "SENTRY_DSN": '{{ get .Secrets "sentry_dsn" }}',
            "STATUS_TOKEN": '{{ get .Secrets "django_status_token" }}',
            "YOUTUBE_DEVELOPER_KEY": '{{ get .Secrets "youtube_developer_key" }}',
        },
        vaultauth=vault_k8s_resources.auth_name,
    )
    secret_names.append(mitlearn_static_secret_name)
    secret_resources.append(mitlearn_static_secret)

    # 2. Static secrets from the shared 'secret-operations' KV-v1 mount
    # These secrets are shared across multiple applications or services.
    secret_ops_configs: list[dict[str, Any]] = [
        {
            "base_name": "secret-ops-embedly",  # Embedly API key
            "path": "global/embedly",
            "templates": {"EMBEDLY_KEY": '{{ get .Secrets "key" }}'},
        },
        {
            "base_name": "secret-ops-odlbot-github",  # GitHub token for odl-bot
            "path": "global/odlbot-github-access-token",
            "templates": {"GITHUB_ACCESS_TOKEN": '{{ get .Secrets "value" }}'},
        },
        {
            "base_name": "secret-ops-mit-smtp",  # MIT SMTP relay credentials
            "path": "global/mit-smtp",
            "templates": {
                "MITOL_EMAIL_HOST": '{{ get .Secrets "relay_host" }}',
                "MITOL_EMAIL_PASSWORD": '{{ get .Secrets "relay_password" }}',
                "MITOL_EMAIL_USER": '{{ get .Secrets "relay_username" }}',
            },
        },
        {
            "base_name": "secret-ops-update-search-webhook",  # Webhook key for search updates
            "path": "global/update-search-data-webhook-key",
            "templates": {
                "OCW_NEXT_SEARCH_WEBHOOK_KEY": '{{ get .Secrets "value" }}',
                "OCW_WEBHOOK_KEY": '{{ get .Secrets "value" }}',
            },
        },
        {
            "base_name": "secret-ops-sso-learn",  # SSO client secret for mitlearn
            "path": "sso/mitlearn",
            "templates": {
                "SOCIAL_AUTH_OL_OIDC_SECRET": '{{ get .Secrets "client_secret" }}'
            },
        },
        {
            "base_name": "secret-ops-tika",  # Tika access token
            "path": "tika/access-token",
            "templates": {"TIKA_ACCESS_TOKEN": '{{ get .Secrets "value" }}'},
        },
    ]

    for config in secret_ops_configs:
        secret_name, secret_resource = _create_static_secret(
            stack_info=stack_info,
            secret_base_name=config["base_name"],
            namespace=mitlearn_namespace,
            labels=k8s_global_labels,
            mount="secret-operations",
            path=config["path"],
            templates=config["templates"],
            vaultauth=vault_k8s_resources.auth_name,
        )
        secret_names.append(secret_name)
        secret_resources.append(secret_resource)

    # 3. Static secrets from the shared 'secret-global' KV-v1 mount
    # Mailgun API key
    secret_global_mailgun_name, secret_global_mailgun = _create_static_secret(
        stack_info=stack_info,
        secret_base_name="secret-global-mailgun",  # Mailgun API key # pragma: allowlist secret  # noqa: S106
        namespace=mitlearn_namespace,
        labels=k8s_global_labels,
        mount="secret-global",
        mount_type="kv-v2",
        path="mailgun",
        templates={"MAILGUN_KEY": '{{ get .Secrets "api_key" }}'},
        vaultauth=vault_k8s_resources.auth_name,
    )
    secret_names.append(secret_global_mailgun_name)
    secret_resources.append(secret_global_mailgun)

    secret_global_hmac_token_name, secret_global_hmac_token = _create_static_secret(
        stack_info=stack_info,
        secret_base_name="secret-global-hmac-token",  # HMAC token # pragma: allowlist secret  # noqa: S106
        namespace=mitlearn_namespace,
        labels=k8s_global_labels,
        mount="secret-global",
        mount_type="kv-v2",
        path="shared_hmac",
        templates={"WEBHOOK_SECRET": '{{ get (get .Secrets "learn") "token" }}'},
        vaultauth=vault_k8s_resources.auth_name,
    )
    secret_names.append(secret_global_hmac_token_name)
    secret_resources.append(secret_global_hmac_token)

    # 4. Dynamic AWS credentials from the 'aws-mitx' backend
    # Provides temporary AWS access keys for the application role.
    aws_access_key_secret_name, aws_access_key_secret = _create_dynamic_secret(
        stack_info=stack_info,
        secret_base_name="aws-secrets",  # AWS credentials  # pragma: allowlist secret  # noqa: S106
        namespace=mitlearn_namespace,
        labels=k8s_global_labels,
        mount="aws-mitx",
        path="creds/ol-mitlearn-application",
        templates={
            "AWS_ACCESS_KEY_ID": '{{ get .Secrets "access_key" }}',
            "AWS_SECRET_ACCESS_KEY": '{{ get .Secrets "secret_key" }}',  # Corrected key name
        },
        vaultauth=vault_k8s_resources.auth_name,
    )
    secret_names.append(aws_access_key_secret_name)
    secret_resources.append(aws_access_key_secret)

    # 5. Dynamic PostgreSQL credentials from the database backend
    # Provides temporary database credentials for the application role.
    database_url_secret_name, database_url_secret = _create_dynamic_secret(
        stack_info=stack_info,
        secret_base_name="psql-secrets",  # PostgreSQL credentials  # pragma: allowlist secret  # noqa: S106
        namespace=mitlearn_namespace,
        labels=k8s_global_labels,
        mount=db_config.db_mount.path,
        path="creds/app",
        templates={
            "DATABASE_URL": f'postgres://{{{{ get .Secrets "username" }}}}:{{{{ get .Secrets "password" }}}}@ol-mitlearn-db-{stack_info.name.lower()}.cbnm7ajau6mi.us-east-1.rds.amazonaws.com:5432/mitopen'
        },
        vaultauth=vault_k8s_resources.auth_name,
    )
    secret_names.append(database_url_secret_name)
    secret_resources.append(database_url_secret)

    # 6. A normal, k8s secret for redis credentials
    # Vault is not needed for these.
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
    secret_names.append(redis_creds_secret_name)
    secret_resources.append(
        redis_creds
    )  # This is different from everything else in the list but allowed per the type hints above

    return secret_names, secret_resources


# OIDC secret is a little different so we do that one with its own function.
def create_oidc_k8s_secret(
    stack_info: StackInfo,
    namespace: str,
    labels: dict[str, str],
    vault_k8s_resources: OLVaultK8SResources,
    opts: ResourceOptions | None = None,
) -> tuple[str, OLVaultK8SSecret]:
    """
    Create the Kubernetes secret containing OIDC configuration from Vault.

    This secret is specifically for the APISIX openid-connect plugin when
    mitlearn is deployed outside of Kubernetes (e.g., Heroku).

    Args:
        stack_info: Information about the current Pulumi stack.
        namespace: Kubernetes namespace where the secret will be created.
        labels: Labels to apply to the Kubernetes secret.
        vault_k8s_resources: Vault Kubernetes auth backend resources.
        opts: Optional Pulumi resource options.

    Returns:
        A tuple containing the generated Kubernetes secret name and the resource object.
    """
    secret_name = "oidc-secrets"  # pragma: allowlist secret # noqa: S105
    resource = OLVaultK8SSecret(
        f"ol-mitlearn-{stack_info.env_suffix}-oidc-secrets",  # Pulumi resource name
        resource_config=OLVaultK8SStaticSecretConfig(
            name="oidc-static-secrets",  # Name of the VaultK8SSecret CRD
            namespace=namespace,
            labels=labels,
            dest_secret_name=secret_name,  # Name of the resulting K8s Secret object
            dest_secret_labels=labels,
            mount="secret-operations",
            mount_type="kv-v1",
            path="sso/mitlearn",
            excludes=[".*"],
            exclude_raw=True,
            # Refresh frequently because substructure keycloak stack could change some of these
            refresh_after="1m",
            templates={
                "client_id": '{{ get .Secrets "client_id" }}',
                "client_secret": '{{ get .Secrets "client_secret" }}',
                "realm": '{{ get .Secrets "realm_name" }}',
                "discovery": '{{ get .Secrets "url" }}/.well-known/openid-configuration',
                "session.secret": '{{ get .Secrets "secret" }}',
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=opts,
    )
    return secret_name, resource
