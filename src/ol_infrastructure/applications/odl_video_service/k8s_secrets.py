# ruff: noqa: E501, PLR0913, S105, S106, FBT001
"""Manage Kubernetes secrets for the ODL Video Service application using Vault.

This module defines functions to create Kubernetes secrets required by the OVS
application by fetching data from various Vault secret backends (static KV and dynamic).
"""

from typing import Literal

import pulumi_kubernetes as kubernetes
from pulumi import Output, ResourceOptions

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
    mount: str | Output[str],
    path: str,
    templates: dict[str, str | Output[str]],
    vaultauth: str,
    mount_type: Literal["kv-v1", "kv-v2"] = "kv-v2",
    opts: ResourceOptions | None = None,
) -> tuple[str, OLVaultK8SSecret]:
    """Create an OLVaultK8SSecret resource for a static Vault secret."""
    secret_name = f"{secret_base_name}-static-secret"
    resource = OLVaultK8SSecret(
        f"ol-ovs-{stack_info.env_suffix}-{secret_base_name}-static-secret",
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
    mount: str | Output[str],
    path: str,
    templates: dict[str, str | Output[str]],
    vaultauth: str,
    opts: ResourceOptions | None = None,
) -> tuple[str, OLVaultK8SSecret]:
    """Create an OLVaultK8SSecret resource for a dynamic Vault secret."""
    secret_name = f"{secret_base_name}-dynamic-secret"
    resource = OLVaultK8SSecret(
        f"ol-ovs-{stack_info.env_suffix}-{secret_base_name}-dynamic-secret",
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


def create_ovs_k8s_secrets(
    stack_info: StackInfo,
    ovs_namespace: str,
    k8s_global_labels: dict[str, str],
    vault_k8s_resources: OLVaultK8SResources,
    db_config: OLVaultDatabaseBackend,
    rds_endpoint: str,
    redis_auth_token: str,
    redis_cluster: OLAmazonCache,
    use_shibboleth: bool,
) -> tuple[list[str], list[OLVaultK8SSecret | kubernetes.core.v1.Secret]]:
    """Create all Kubernetes secrets required by the ODL Video Service application.

    Args:
        stack_info: Information about the current Pulumi stack.
        ovs_namespace: The Kubernetes namespace for OVS resources.
        k8s_global_labels: Standard labels to apply to all Kubernetes resources.
        vault_k8s_resources: Vault Kubernetes auth backend resources.
        db_config: Configuration for the Vault dynamic PostgreSQL database backend.
        rds_endpoint: The endpoint address of the RDS instance (host:port).
        redis_auth_token: The auth token for the Redis cluster.
        redis_cluster: The Redis cache resource for connection details.
        use_shibboleth: Whether Shibboleth is enabled for this environment.

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
        secret_base_name="aws-ovs",  # pragma: allowlist secret
        namespace=ovs_namespace,
        labels=k8s_global_labels,
        mount="aws-mitx",
        path="creds/ovs-server",
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
        secret_base_name="postgres-ovs",  # pragma: allowlist secret
        namespace=ovs_namespace,
        labels=k8s_global_labels,
        mount=db_config.db_mount.path,
        path="creds/app",
        templates={
            "DATABASE_URL": f'postgres://{{{{ get .Secrets "username" }}}}:{{{{ get .Secrets "password" }}}}@{rds_endpoint}/odlvideo'
        },
        vaultauth=vaultauth,
    )
    secret_names.append(db_secret_name)
    secret_resources.append(db_secret)

    # 3. Redis credentials (plain K8s secret, value depends on redis cluster address)
    redis_creds_secret_name = "redis-creds"  # pragma: allowlist secret
    redis_creds = kubernetes.core.v1.Secret(
        f"ovs-{stack_info.env_suffix}-redis-creds",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=redis_creds_secret_name,
            namespace=ovs_namespace,
            labels=k8s_global_labels,
        ),
        string_data=redis_cluster.address.apply(
            lambda address: {
                "CELERY_BROKER_URL": f"rediss://default:{redis_auth_token}@{address}:{DEFAULT_REDIS_PORT}/0?ssl_cert_reqs=required",
                "REDIS_URL": f"rediss://default:{redis_auth_token}@{address}:{DEFAULT_REDIS_PORT}/0?ssl_cert_reqs=required",
            }
        ),
        opts=ResourceOptions(
            depends_on=[redis_cluster],
            delete_before_replace=True,
        ),
    )
    secret_names.append(redis_creds_secret_name)
    secret_resources.append(redis_creds)

    # 4. Static secrets from 'secret-odl-video-service' KV-v2 mount (all app secrets)
    ovs_secrets_name, ovs_secrets = _create_static_secret(
        stack_info=stack_info,
        secret_base_name="ovs-app-secrets",  # pragma: allowlist secret
        namespace=ovs_namespace,
        labels=k8s_global_labels,
        mount="secret-odl-video-service",
        mount_type="kv-v2",
        path="ovs-secrets",
        templates={
            "VIDEO_CLOUDFRONT_DIST": '{{ index .Secrets "cloudfront" "subdomain" }}',
            "DROPBOX_KEY": '{{ index .Secrets "dropbox" "key" }}',
            "DROPBOX_TOKEN": '{{ index .Secrets "dropbox" "token" }}',
            "ET_PIPELINE_ID": '{{ index .Secrets "misc" "et_pipeline_id" }}',
            "FIELD_ENCRYPTION_KEY": '{{ index .Secrets "misc" "field_encryption_key" }}',
            "MIT_WS_CERTIFICATE": '{{ index .Secrets "misc" "mit_ws_certificate" }}',
            "MIT_WS_PRIVATE_KEY": '{{ index .Secrets "misc" "mit_ws_private_key" }}',
            "SECRET_KEY": '{{ index .Secrets "misc" "secret_key" }}',
            "MAILGUN_URL": '{{ index .Secrets "mailgun" "url" }}',
            "OPENEDX_API_CLIENT_ID": '{{ index .Secrets "openedx" "api_client_id" }}',
            "OPENEDX_API_CLIENT_SECRET": '{{ index .Secrets "openedx" "api_client_secret" }}',
            "SENTRY_DSN": '{{ index .Secrets "sentry" "dsn" }}',
            "GA_VIEW_ID": '{{ index .Secrets "google_analytics" "id" }}',
            "GA_KEYFILE_JSON": '{{ index .Secrets "google_analytics" "json" }}',
            "GA_TRACKING_ID": '{{ index .Secrets "google_analytics" "tracking_id" }}',
            "YT_ACCESS_TOKEN": '{{ index .Secrets "youtube" "access_token" }}',
            "YT_CLIENT_ID": '{{ index .Secrets "youtube" "client_id" }}',
            "YT_CLIENT_SECRET": '{{ index .Secrets "youtube" "client_secret" }}',
            "YT_PROJECT_ID": '{{ index .Secrets "youtube" "project_id" }}',
            "YT_REFRESH_TOKEN": '{{ index .Secrets "youtube" "refresh_token" }}',
            "VIDEO_S3_TRANSCODE_ENDPOINT": '{{ get .Secrets "transcode_endpoint" }}',
        },
        vaultauth=vaultauth,
    )
    secret_names.append(ovs_secrets_name)
    secret_resources.append(ovs_secrets)

    # 5. Static secrets from 'secret-operations' for CloudFront private key
    cloudfront_secret_name, cloudfront_secret = _create_static_secret(
        stack_info=stack_info,
        secret_base_name="ovs-cloudfront",  # pragma: allowlist secret
        namespace=ovs_namespace,
        labels=k8s_global_labels,
        mount="secret-operations",
        mount_type="kv-v1",
        path="global/cloudfront-private-key",
        templates={
            "CLOUDFRONT_KEY_ID": '{{ get .Secrets "id" }}',
            "CLOUDFRONT_PRIVATE_KEY": '{{ get .Secrets "value" }}',
        },
        vaultauth=vaultauth,
    )
    secret_names.append(cloudfront_secret_name)
    secret_resources.append(cloudfront_secret)

    # 6. Static secrets from 'secret-operations' for Mailgun API key
    mailgun_secret_name, mailgun_secret = _create_static_secret(
        stack_info=stack_info,
        secret_base_name="ovs-mailgun",  # pragma: allowlist secret
        namespace=ovs_namespace,
        labels=k8s_global_labels,
        mount="secret-operations",
        mount_type="kv-v1",
        path="global/mailgun-api-key",
        templates={
            "MAILGUN_KEY": '{{ get .Secrets "value" }}',
        },
        vaultauth=vaultauth,
    )
    secret_names.append(mailgun_secret_name)
    secret_resources.append(mailgun_secret)

    # 7. Shibboleth SP cert/key secrets (only when Shibboleth is enabled)
    if use_shibboleth:
        shib_certs_secret_name, shib_certs_secret = _create_static_secret(
            stack_info=stack_info,
            secret_base_name="ovs-shib-certs",  # pragma: allowlist secret
            namespace=ovs_namespace,
            labels=k8s_global_labels,
            mount="secret-odl-video-service",
            mount_type="kv-v2",
            path="ovs-secrets",
            templates={
                "sp-cert.pem": '{{ index .Secrets "shibboleth" "sp_cert" }}',
                "sp-key.pem": '{{ index .Secrets "shibboleth" "sp_key" }}',
                "mit-md-cert.pem": '{{ index .Secrets "shibboleth" "mit_md_cert" }}',
            },
            vaultauth=vaultauth,
        )
        secret_names.append(shib_certs_secret_name)
        secret_resources.append(shib_certs_secret)

    return secret_names, secret_resources
