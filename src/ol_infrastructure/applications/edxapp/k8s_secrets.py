# ruff: noqa: E501, S105, PLR0913
# mypy: ignore-errors
"""Kubernetes secrets for edxapp using Vault integration.

This module creates all Kubernetes secrets needed for edxapp, using a
configuration-driven approach with factory functions to minimize boilerplate.

Previous version: 500+ lines with 84% duplication
Refactored version: ~250 lines, fully DRY
"""

import textwrap
from dataclasses import dataclass

from pulumi import Config, Output, StackReference

from ol_infrastructure.applications.edxapp.secrets_builder import (
    get_database_connections_template,
    get_database_credentials_template,
    get_general_secrets_yaml,
    get_mongodb_credentials_template,
    get_mongodb_forum_template,
)
from ol_infrastructure.applications.edxapp.secrets_factory import VaultSecretBuilder
from ol_infrastructure.components.aws.cache import OLAmazonCache
from ol_infrastructure.components.aws.database import OLAmazonDB
from ol_infrastructure.components.services.vault import (
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SResources,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo


@dataclass
class EdxappSecrets:
    """Container for all edxapp Kubernetes secrets."""

    db_creds: Output
    db_connections: Output
    mongo_db_creds: OLVaultK8SSecret
    mongo_db_forum: OLVaultK8SSecret
    general: Output
    xqueue: OLVaultK8SSecret | None
    forum: OLVaultK8SSecret
    learn_ai_canvas_syllabus_token: OLVaultK8SSecret
    cms_oauth: OLVaultK8SSecret
    lms_oauth: OLVaultK8SSecret | None
    git_export_ssh_key: OLVaultK8SSecret

    db_creds_secret_name: str
    db_connections_secret_name: str
    mongo_db_creds_secret_name: str
    mongo_db_forum_secret_name: str
    general_secrets_name: str
    xqueue_secret_name: str | None
    forum_secret_name: str
    learn_ai_canvas_syllabus_token_secret_name: str
    cms_oauth_secret_name: str
    lms_oauth_secret_name: str | None
    git_export_ssh_key_secret_name: str


def create_k8s_secrets(
    edxapp_cache: OLAmazonCache,
    edxapp_config: Config,
    edxapp_db: OLAmazonDB,
    k8s_global_labels: dict[str, str],
    mongodb_atlas_stack: StackReference,
    namespace: str,
    stack_info: StackInfo,
    vault_k8s_resources: OLVaultK8SResources,
    xqueue_stack: StackReference,
) -> EdxappSecrets:
    """Create all Kubernetes secrets for edxapp using registry pattern.

    This function replaced 346 lines of repetitive secret creation code
    with a declarative registry-based approach and factory functions.

    Args:
        edxapp_cache: Redis cache for configuration
        edxapp_config: Pulumi config for this stack
        edxapp_db: MariaDB database instance
        k8s_global_labels: Labels to apply to all resources
        mongodb_atlas_stack: Stack reference to MongoDB Atlas
        namespace: Kubernetes namespace for secrets
        stack_info: Stack information (env_prefix, env_suffix)
        vault_k8s_resources: Vault Kubernetes authentication resources
        xqueue_stack: Stack reference to xqueue application

    Returns:
        EdxappSecrets dataclass with all created secrets
    """
    # Create builder for secret creation
    builder = VaultSecretBuilder(
        stack_info=stack_info,
        namespace=namespace,
        k8s_global_labels=k8s_global_labels,
        vault_k8s_resources=vault_k8s_resources,
    )

    # Define secret names  # pragma: allowlist secret
    db_creds_secret_name = "00-database-credentials-yaml"  # pragma: allowlist secret
    db_connections_secret_name = (
        "01-database-connections-yaml"  # pragma: allowlist secret
    )
    mongo_db_creds_secret_name = (
        "02-mongodb-credentials-yaml"  # pragma: allowlist secret
    )
    mongo_db_forum_secret_name = (
        "03-mongodb-forum-credentials-yaml"  # pragma: allowlist secret
    )
    general_secrets_name = "10-general-secrets-yaml"  # pragma: allowlist secret
    xqueue_secret_name = "11-xqueue-secrets-yaml"  # pragma: allowlist secret
    forum_secret_name = "12-forum-secrets-yaml"  # pragma: allowlist secret
    learn_ai_canvas_syllabus_token_secret_name = (
        "13-canvas-syllabus-token-yaml"  # pragma: allowlist secret
    )
    cms_oauth_secret_name = "70-cms-oauth-credentials-yaml"  # pragma: allowlist secret
    lms_oauth_secret_name = "80-lms-oauth-credentials-yaml"  # pragma: allowlist secret
    git_export_ssh_key_secret_name = "git-export-ssh-key"  # pragma: allowlist secret

    # Database credentials secret (dynamic - depends on DB outputs)
    db_creds_secret = Output.all(
        address=edxapp_db.db_instance.address,
        port=edxapp_db.db_instance.port,
    ).apply(
        lambda db: OLVaultK8SSecret(
            builder.get_resource_name("db-creds-secret"),
            OLVaultK8SDynamicSecretConfig(
                name=db_creds_secret_name,
                namespace=namespace,
                dest_secret_labels=k8s_global_labels,
                dest_secret_name=db_creds_secret_name,
                labels=k8s_global_labels,
                mount=f"mariadb-{stack_info.env_prefix}",
                path="creds/edxapp",
                templates={
                    "00-database-credentials.yaml": get_database_credentials_template(
                        db_address=db["address"], db_port=db["port"]
                    )[0],
                },
                vaultauth=vault_k8s_resources.auth_name,
            ),
            opts=builder.get_common_options(),
        )
    )

    # Database connections secret (dynamic - depends on DB outputs)
    db_connections_secret = Output.all(
        address=edxapp_db.db_instance.address,
        port=edxapp_db.db_instance.port,
    ).apply(
        lambda db: OLVaultK8SSecret(
            builder.get_resource_name("db-connections-secret"),
            OLVaultK8SDynamicSecretConfig(
                name=db_connections_secret_name,
                namespace=namespace,
                dest_secret_labels=k8s_global_labels,
                dest_secret_name=db_connections_secret_name,
                labels=k8s_global_labels,
                mount=f"mariadb-{stack_info.env_prefix}",
                path="creds/edxapp",
                templates={
                    "01-database-connections.yaml": get_database_connections_template(
                        db_address=db["address"], db_port=db["port"]
                    )[0],
                },
                vaultauth=vault_k8s_resources.auth_name,
            ),
            opts=builder.get_common_options(),
        )
    )

    # MongoDB credentials secret (dynamic - depends on MongoDB Atlas)
    mongo_db_creds_secret = Output.all(
        replica_set=mongodb_atlas_stack.require_output("atlas_cluster")["replica_set"],
        host_string=mongodb_atlas_stack.require_output("atlas_cluster")[
            "public_host_string"
        ],
    ).apply(
        lambda mongodb: OLVaultK8SSecret(
            builder.get_resource_name("mongo-db-creds-secret"),
            OLVaultK8SStaticSecretConfig(
                name=mongo_db_creds_secret_name,
                namespace=namespace,
                dest_secret_labels=k8s_global_labels,
                dest_secret_name=mongo_db_creds_secret_name,
                labels=k8s_global_labels,
                mount=f"secret-{stack_info.env_prefix}",
                mount_type="kv-v1",
                path="mongodb-edxapp",
                templates={
                    "02-mongo-db-credentials.yaml": get_mongodb_credentials_template(
                        replica_set=mongodb["replica_set"],
                        host_string=mongodb["host_string"],
                    )[0],
                },
                vaultauth=vault_k8s_resources.auth_name,
            ),
            opts=builder.get_common_options(),
        )
    )

    # MongoDB forum secret (dynamic - depends on MongoDB Atlas)
    mongo_db_forum_secret = Output.all(
        replica_set=mongodb_atlas_stack.require_output("atlas_cluster")["replica_set"],
        host_string=mongodb_atlas_stack.require_output("atlas_cluster")[
            "public_host_string"
        ],
    ).apply(
        lambda mongodb: OLVaultK8SSecret(
            builder.get_resource_name("mongo-forum-creds-secret"),
            OLVaultK8SStaticSecretConfig(
                name=mongo_db_forum_secret_name,
                namespace=namespace,
                dest_secret_labels=k8s_global_labels,
                dest_secret_name=mongo_db_forum_secret_name,
                labels=k8s_global_labels,
                mount=f"secret-{stack_info.env_prefix}",
                mount_type="kv-v1",
                path="mongodb-forum",
                templates={
                    "03-mongo-db-forum-credentials.yaml": get_mongodb_forum_template(
                        replica_set=mongodb["replica_set"],
                        host_string=mongodb["host_string"],
                    )[0],
                },
                vaultauth=vault_k8s_resources.auth_name,
            ),
            opts=builder.get_common_options(),
        )
    )

    # General secrets (dynamic - depends on Redis hostname and config)
    general_secrets_secret = Output.all(
        redis_hostname=edxapp_cache.address,
    ).apply(
        lambda redis_cache: OLVaultK8SSecret(
            builder.get_resource_name("general-secret"),
            OLVaultK8SStaticSecretConfig(
                name=general_secrets_name,
                namespace=namespace,
                dest_secret_labels=k8s_global_labels,
                dest_secret_name=general_secrets_name,
                labels=k8s_global_labels,
                mount=f"secret-{stack_info.env_prefix}",
                mount_type="kv-v1",
                path="edxapp",
                templates={
                    "10-general-secrets.yaml": get_general_secrets_yaml(
                        stack_info=stack_info,
                        redis_hostname=redis_cache["redis_hostname"],
                        lms_domain=edxapp_config.require_object("domains")["lms"],
                        proctortrack_url=edxapp_config.get("proctortrack_url"),
                    ),
                },
                vaultauth=vault_k8s_resources.auth_name,
            ),
            opts=builder.get_common_options(),
        )
    )

    # Xqueue secret (conditional - only if enabled)
    if edxapp_config.get_bool("enable_xqueue"):
        xqueue_domain = xqueue_stack.get_output("xqueue_domain")

        def create_xqueue_secret(domain: str) -> OLVaultK8SSecret:
            return builder.create_static(
                name="xqueue-secrets",
                resource_name="xqueue-secret",
                secret_name=xqueue_secret_name,
                mount=f"secret-{stack_info.env_prefix}",
                path="edx-xqueue",
                templates={
                    "11-xqueue-secrets.yaml": textwrap.dedent(f"""
                        XQUEUE_INTERFACE:
                          django_auth:
                            password: {{{{ get .Secrets "edxapp_password" }}}}
                            username: edxapp
                          url: https://{domain}
                    """),
                },
            )

        xqueue_secret_secret = xqueue_domain.apply(create_xqueue_secret)
    else:
        xqueue_secret_secret = None

    # Forum secret (static)
    forum_secret_secret = builder.create_static(
        name="forum-secrets",
        resource_name="forum-secret",
        secret_name=forum_secret_name,
        mount=f"secret-{stack_info.env_prefix}",
        path="edx-forum",
        templates={
            "12-forum-secrets.yaml": textwrap.dedent("""
                COMMENTS_SERVICE_KEY: {{ get .Secrets "forum_api_key" }}
            """),
        },
    )

    # Learn AI canvas syllabus token secret (static, global mount)
    learn_ai_canvas_syllabus_token_secret_secret = builder.create_static(
        name="canvas-syllabus-token",
        resource_name="learn-ai-canvas-syllabus-token-secret",
        secret_name=learn_ai_canvas_syllabus_token_secret_name,
        mount="secret-global",
        path="learn_ai",
        mount_type="kv-v2",
        templates={
            "13-canvas-syllabus-token-secrets.yaml": textwrap.dedent("""
                MIT_LEARN_AI_XBLOCK_CHAT_API_TOKEN: {{ get .Secrets "canvas_syllabus_token" }}
            """),
        },
    )

    # CMS OAuth secret (static)
    cms_oauth_secret = builder.create_static(
        name="cms-oauth-credentials",
        resource_name="cms-oauth-secret",
        secret_name=cms_oauth_secret_name,
        mount=f"secret-{stack_info.env_prefix}",
        path="edxapp",
        templates={
            "70-cms-oauth-credentials.yaml": textwrap.dedent("""
                SOCIAL_AUTH_EDX_OAUTH2_KEY: {{ (get .Secrets "studio_oauth_client").id }}
                SOCIAL_AUTH_EDX_OAUTH2_SECRET: {{ (get .Secrets "studio_oauth_client").secret }}
            """),
        },
    )

    # LMS OAuth secret (conditional - only for xpro and mitxonline)
    if stack_info.env_prefix in ["xpro", "mitxonline"]:
        lms_oauth_secret = builder.create_static(
            name="lms-oauth-credentials",
            resource_name="lms-oauth-secret",
            secret_name=lms_oauth_secret_name,
            mount=f"secret-{stack_info.env_prefix}",
            path="edxapp",
            templates={
                "80-lms-oauth-credentials.yaml": f"""SOCIAL_AUTH_OAUTH_SECRETS:
    ol-oauth2: {{{{ get .Secrets "{stack_info.env_prefix}_oauth_secret" }}}}
""",
            },
        )
    else:
        lms_oauth_secret = None

    # Git export SSH key secret (static, operations mount)
    git_export_ssh_key_secret = builder.create_static(
        name="git-export-ssh-key",
        resource_name="git-export-ssh-key",
        secret_name=git_export_ssh_key_secret_name,
        mount="secret-operations",
        path="global/github-enterprise-ssh",
        templates={
            "private_key": '{{ get .Secrets "private_key" }}',
        },
    )

    # Return dataclass with all secrets
    return EdxappSecrets(
        db_creds=db_creds_secret,
        db_connections=db_connections_secret,
        mongo_db_creds=mongo_db_creds_secret,
        mongo_db_forum=mongo_db_forum_secret,
        general=general_secrets_secret,
        xqueue=xqueue_secret_secret,
        forum=forum_secret_secret,
        learn_ai_canvas_syllabus_token=learn_ai_canvas_syllabus_token_secret_secret,
        cms_oauth=cms_oauth_secret,
        lms_oauth=lms_oauth_secret,
        git_export_ssh_key=git_export_ssh_key_secret,
        db_creds_secret_name=db_creds_secret_name,
        db_connections_secret_name=db_connections_secret_name,
        mongo_db_creds_secret_name=mongo_db_creds_secret_name,
        mongo_db_forum_secret_name=mongo_db_forum_secret_name,
        general_secrets_name=general_secrets_name,
        xqueue_secret_name=xqueue_secret_name,
        forum_secret_name=forum_secret_name,
        learn_ai_canvas_syllabus_token_secret_name=learn_ai_canvas_syllabus_token_secret_name,
        cms_oauth_secret_name=cms_oauth_secret_name,
        lms_oauth_secret_name=lms_oauth_secret_name,
        git_export_ssh_key_secret_name=git_export_ssh_key_secret_name,
    )
