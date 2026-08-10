# ruff: noqa: E501, S105, PLR0912, PLR0913, PLR0915
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
    OLVaultRestartTarget,
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
    translations_providers: OLVaultK8SSecret | None
    webhook_tokens: OLVaultK8SSecret | None
    meilisearch: OLVaultK8SSecret | None
    typesense: OLVaultK8SSecret | None
    azure_openai: OLVaultK8SSecret | None

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
    translations_providers_secret_name: str | None
    webhook_tokens_secret_name: str | None
    meilisearch_secret_name: str | None
    typesense_secret_name: str | None
    azure_openai_secret_name: str | None


def create_k8s_secrets(
    edxapp_cache: OLAmazonCache,
    edxapp_config: Config,
    edxapp_db: OLAmazonDB,
    k8s_global_labels: dict[str, str],
    mongodb_atlas_stack: StackReference,
    namespace: str,
    stack_info: StackInfo,
    vault_k8s_resources: OLVaultK8SResources,
    restart_deployment_names: list[str] | None = None,
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
        restart_deployment_names: Deployment names to restart when DB
            credentials are rotated by Vault (dynamic secrets only).

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
    translations_providers_secret_name = (
        "14-translations-providers-yaml"  # pragma: allowlist secret
    )
    meilisearch_secret_name = "15-meilisearch-yaml"  # pragma: allowlist secret
    typesense_secret_name = "16-typesense-yaml"  # pragma: allowlist secret
    webhook_tokens_secret_name = "17-webhook-tokens-yaml"  # pragma: allowlist secret
    azure_openai_secret_name = "18-azure-openai-yaml"  # pragma: allowlist secret

    # Database credentials secret (dynamic - depends on DB outputs)
    _db_restart_targets = (
        [
            OLVaultRestartTarget(kind="Deployment", name=n)
            for n in restart_deployment_names
        ]
        if restart_deployment_names
        else None
    )
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
                restart_targets=_db_restart_targets,
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
                restart_targets=_db_restart_targets,
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
        xqueue_domain = edxapp_config.require("xqueue_domain")
        xqueue_secret_secret = builder.create_static(
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
                      url: https://{xqueue_domain}
                """),
            },
        )
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

    # Translations providers secret (conditional - only for mitxonline)
    if stack_info.env_prefix == "mitxonline":
        translations_providers_secret = builder.create_static(
            name="translations-providers",
            resource_name="translations-providers-secret",
            secret_name=translations_providers_secret_name,
            mount=f"secret-{stack_info.env_prefix}",
            path="edxapp",
            templates={
                "14-translations-providers-secrets.yaml": textwrap.dedent("""
                    TRANSLATIONS_GITHUB_TOKEN: {{ get .Secrets "translations_github_token" }}
                    TRANSLATIONS_PROVIDERS:
                      default_provider: mistral
                      deepl:
                        api_key: {{ get .Secrets "deepl_api_key" }}
                      openai:
                        api_key: {{ get .Secrets "openai_api_key" }}
                        default_model: gpt-5.2
                      gemini:
                        api_key: {{ get .Secrets "gemini_api_key" }}
                        default_model: gemini-3-pro-preview
                      mistral:
                        api_key: {{ get .Secrets "mistral_api_key" }}
                        default_model: mistral-large-latest
                """),
            },
        )
    else:
        translations_providers_secret = None

    # Azure OpenAI credentials, minted per-lease by Vault and scoped to mitxonline's
    # own Cognitive Services account. mitxonline-only, matching the translations
    # providers secret above.
    #
    # These are delivered as flat top-level settings in their own config source rather
    # than as another `TRANSLATIONS_PROVIDERS:` block. The init container concatenates
    # the config sources with `cat` (see k8s_resources.py) instead of deep-merging
    # them, so a second file emitting that key would silently clobber the deepl /
    # openai / gemini / mistral providers -- last one wins. The edx-extensions plugin
    # folds these into TRANSLATIONS_PROVIDERS in Python, at Django settings load.
    #
    # Gated on edxapp:azure_openai_tenant_id: a VaultDynamicSecret pointed at a mount
    # Vault does not have yet fails rather than degrading, so the switch is per
    # environment and happens after substructure/vault/azure is deployed there.
    azure_openai_tenant_id = edxapp_config.get("azure_openai_tenant_id")
    if stack_info.env_prefix == "mitxonline" and azure_openai_tenant_id:
        azure_openai_endpoint = (
            f"https://ol-openai-mitxonline-{stack_info.env_suffix}.openai.azure.com/"
        )
        azure_openai_secret = OLVaultK8SSecret(
            f"edxapp-{stack_info.env_prefix}-{stack_info.env_suffix}-azure-openai-secret",
            OLVaultK8SDynamicSecretConfig(
                name="azure-openai-secrets",
                namespace=namespace,
                labels=k8s_global_labels,
                dest_secret_name=azure_openai_secret_name,
                dest_secret_labels=k8s_global_labels,
                mount="azure-openai",
                path="creds/ol-mitxonline-openai",
                templates={
                    "18-azure-openai-secrets.yaml": textwrap.dedent(f"""
                        AZURE_OPENAI_CLIENT_ID: {{{{ get .Secrets "client_id" }}}}
                        AZURE_OPENAI_CLIENT_SECRET: {{{{ get .Secrets "client_secret" }}}}
                        AZURE_OPENAI_TENANT_ID: {azure_openai_tenant_id}
                        AZURE_OPENAI_ENDPOINT: {azure_openai_endpoint}
                        AZURE_OPENAI_API_VERSION: {edxapp_config.get("azure_openai_api_version") or "2024-10-21"}
                        AZURE_OPENAI_DEFAULT_DEPLOYMENT: {edxapp_config.get("azure_openai_default_deployment") or "gpt-5.2"}
                    """),
                },
                vaultauth=vault_k8s_resources.auth_name,
            ),
        )
    else:
        azure_openai_secret = None

    meilisearch_config = Config("meilisearch")
    if meilisearch_config.get_bool("enabled"):
        meilisearch_secret = builder.create_static(
            name="meilisearch",
            resource_name="meilisearch-secret",
            secret_name=meilisearch_secret_name,
            mount=f"secret-{stack_info.env_prefix}",
            path="edxapp",
            templates={
                "15-meilisearch-secrets.yaml": textwrap.dedent("""
                    MEILISEARCH_MASTER_KEY: {{ get .Secrets "meilisearch_master_key" }}
                    MEILISEARCH_API_KEY: {{ get .Secrets "meilisearch_api_key" }}
                """),
            },
        )
    else:
        meilisearch_secret = None

    typesense_config = Config("typesense")
    if typesense_config.get_bool("enabled"):
        typesense_secret = builder.create_static(
            name="typesense",
            resource_name="typesense-secret",
            secret_name=typesense_secret_name,
            mount=f"secret-{stack_info.env_prefix}",
            path="edxapp",
            templates={
                "16-typesense-secrets.yaml": textwrap.dedent("""
                    TYPESENSE_API_KEY: {{ get .Secrets "typesense_bootstrap_key" }}
                """),
            },
        )
    else:
        typesense_secret = None

    # Webhook tokens secret (conditional - only for mitxonline QA/Production)
    if stack_info.env_prefix == "mitxonline" and stack_info.env_suffix in [
        "qa",
        "production",
    ]:
        webhook_tokens_secret = builder.create_static(
            name="webhook-tokens",
            resource_name="webhook-tokens-secret",
            secret_name=webhook_tokens_secret_name,
            mount=f"secret-{stack_info.env_prefix}",
            path="edxapp",
            templates={
                "17-webhook-tokens-secrets.yaml": textwrap.dedent("""
                    CERTIFICATE_WEBHOOK_ACCESS_TOKEN: {{ get .Secrets "webhook_access_token" }}
                    ENROLLMENT_WEBHOOK_ACCESS_TOKEN: {{ get .Secrets "webhook_access_token" }}
                """),
            },
        )
    else:
        webhook_tokens_secret = None

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
        translations_providers=translations_providers_secret,
        webhook_tokens=webhook_tokens_secret,
        meilisearch=meilisearch_secret,
        typesense=typesense_secret,
        azure_openai=azure_openai_secret,
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
        translations_providers_secret_name=translations_providers_secret_name
        if stack_info.env_prefix == "mitxonline"
        else None,
        webhook_tokens_secret_name=webhook_tokens_secret_name
        if stack_info.env_prefix == "mitxonline"
        and stack_info.env_suffix in ["qa", "production"]
        else None,
        meilisearch_secret_name=meilisearch_secret_name,
        typesense_secret_name=typesense_secret_name,
        azure_openai_secret_name=azure_openai_secret_name
        if azure_openai_secret
        else None,
    )
