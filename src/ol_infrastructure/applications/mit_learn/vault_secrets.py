"""MIT Learn's Vault-backed environment variables.

The deployed program creates VSO resources from this (k8s_secrets.py); a local
program can build its Secret's keys from ``secret_env_names``.
"""

from pulumi import Output

from ol_infrastructure.lib.opik import (
    OPIK_KEYCLOAK_SECRET_TEMPLATES,
    OPIK_SDK_VAULT_MOUNT,
    opik_sdk_vault_path,
)
from ol_infrastructure.lib.secret_env import Template, VaultSecret, env_names

OPIK_SECRET = "secret-opik-sdk-client"  # noqa: S105  # pragma: allowlist secret
MEMORY_SECRET = "secret-ops-sso-mitlearn-memory"  # noqa: S105  # pragma: allowlist secret
STARROCKS_SECRET = "starrocks-secrets"  # noqa: S105  # pragma: allowlist secret


def vault_secrets(
    *,
    mitlearn_mount: str | Output[str],
    database_mount: str | Output[str],
    database_host: str,
    starrocks_mount: str | None,
    memory_enabled: bool,
) -> list[VaultSecret]:
    """Describe every Vault path the app reads and the variables it supplies.

    :param mitlearn_mount: The app's own KV v2 mount.
    :param database_mount: The Vault database engine's mount.
    :param database_host: ``host:port`` of the app's database.
    :param starrocks_mount: The Vault StarRocks database engine's mount, or None
        for a stack that does not read the warehouse.
    :param memory_enabled: Whether the stack runs the memory service, whose
        token-exchange client only exists there.
    """
    # ol-mitlearn-memory-client, which ol-llm's memory interface exchanges a
    # learner's session token with. The keycloak substructure writes it
    # (ol_memory.py). Required, unlike the Opik client: the rest of the memory
    # settings are plain env, and MemoryConfig.from_env accepts them with no
    # exchange pair, so a pod that started before VSO's first sync would hand
    # raw session tokens to Hindsight and get 401 on every call.
    memory = (
        [
            VaultSecret(
                name=MEMORY_SECRET,
                kind="static",
                mount="secret-operations",
                mount_type="kv-v1",
                path="sso/mitlearn-memory",
                env={
                    "OL_LLM_MEMORY_EXCHANGE_CLIENT_ID": ("client_id",),
                    "OL_LLM_MEMORY_EXCHANGE_CLIENT_SECRET": ("client_secret",),
                },
            )
        ]
        if memory_enabled
        else []
    )
    # Read by learning_resources.lib.warehouse to query the
    # integrations__learn__* views.
    starrocks = (
        [
            VaultSecret(
                name=STARROCKS_SECRET,
                kind="dynamic",
                mount=starrocks_mount,
                path="creds/batch_readonly",
                env={
                    "STARROCKS_USER": ("username",),
                    "STARROCKS_PASSWORD": ("password",),
                },
            )
        ]
        if starrocks_mount
        else []
    )
    return [
        VaultSecret(
            name="mitopen",
            kind="static",
            mount=mitlearn_mount,
            mount_type="kv-v2",
            path="secrets",
            env={
                "FASTLY_API_KEY": ("fastly", "api_key"),
                "FASTLY_SERVICE_ID": ("fastly", "service_id"),
                "CKEDITOR_ENVIRONMENT_ID": ("ckeditor", "environment_id"),
                "CKEDITOR_SECRET_KEY": ("ckeditor", "secret_key"),
                "CKEDITOR_UPLOAD_URL": ("ckeditor", "upload_url"),
                "EDX_API_CLIENT_ID": ("edx_api_client", "id"),
                "EDX_API_CLIENT_SECRET": ("edx_api_client", "secret"),
                "OLL_API_CLIENT_ID": ("open_learning_library_client", "client_id"),
                "OLL_API_CLIENT_SECRET": (
                    "open_learning_library_client",
                    "client_secret",
                ),
                "OPENAI_API_KEY": ("openai", "api_key"),
                "OPENSEARCH_HTTP_AUTH": ("opensearch", "http_auth"),
                "QDRANT_API_KEY": ("qdrant", "api_key_v2"),
                "QDRANT_API_KEY_V2": ("qdrant", "api_key_v2"),
                "POSTHOG_PROJECT_API_KEY": ("posthog", "project_api_key"),
                "POSTHOG_PERSONAL_API_KEY": ("posthog", "personal_api_key"),
                "RECAPTCHA_SECRET_KEY": ("recaptcha", "secret_key"),
                "RECAPTCHA_SITE_KEY": ("recaptcha", "site_key"),
                "SEE_API_CLIENT_ID": ("see_api_client", "id"),
                "SEE_API_CLIENT_SECRET": ("see_api_client", "secret"),
                "MITOL_JWT_SECRET": ("jwt_secret",),
                "MITX_ONLINE_ETL_API_KEY": ("mitxonline", "etl_api_key"),
                "SECRET_KEY": ("django_secret_key",),
                "SENTRY_DSN": ("sentry_dsn",),
                "STATUS_TOKEN": ("django_status_token",),
                "UNSUBSCRIBE_SECRET_KEY": ("unsubscribe_secret_key",),
                "YOUTUBE_DEVELOPER_KEY": ("youtube_developer_key",),
            },
        ),
        VaultSecret(
            name="secret-ops-embedly",
            kind="static",
            mount="secret-operations",
            mount_type="kv-v1",
            path="global/embedly",
            env={"EMBEDLY_KEY": ("key",)},
        ),
        VaultSecret(
            name="secret-ops-mit-smtp",
            kind="static",
            mount="secret-operations",
            mount_type="kv-v1",
            path="global/mit-smtp",
            env={
                "MITOL_EMAIL_HOST": ("relay_host",),
                "MITOL_EMAIL_PASSWORD": ("relay_password",),
                "MITOL_EMAIL_USER": ("relay_username",),
            },
        ),
        VaultSecret(
            name="secret-ops-update-search-webhook",
            kind="static",
            mount="secret-operations",
            mount_type="kv-v1",
            path="global/update-search-data-webhook-key",
            env={
                "OCW_NEXT_SEARCH_WEBHOOK_KEY": ("value",),
                "OCW_WEBHOOK_KEY": ("value",),
            },
        ),
        VaultSecret(
            name="secret-ops-sso-learn",
            kind="static",
            mount="secret-operations",
            mount_type="kv-v1",
            path="sso/mitlearn",
            env={
                "SOCIAL_AUTH_OL_OIDC_SECRET": ("client_secret",),
                # Starts Keycloak account actions (change email / change
                # password). Same client APISIX authenticates with, so its
                # registered redirect URIs already cover the callback. Only the
                # id: the redirect is unauthenticated and mit-learn never
                # exchanges a token with this client.
                "KEYCLOAK_CLIENT_ID": ("client_id",),
            },
        ),
        # Keycloak Admin API service account, provisioned by the keycloak
        # substructure stack (olapps.py, mitlearn-admin-client).
        VaultSecret(
            name="secret-ops-keycloak-admin-learn",
            kind="static",
            mount="secret-operations",
            mount_type="kv-v1",
            path="sso/mitlearn-admin",
            env={
                "MITOL_KEYCLOAK_ADMIN_CLIENT_ID": ("client_id",),
                "MITOL_KEYCLOAK_ADMIN_CLIENT_SECRET": ("client_secret",),
            },
        ),
        VaultSecret(
            name="secret-ops-tika",
            kind="static",
            mount="secret-operations",
            mount_type="kv-v1",
            path="tika/access-token",
            env={"TIKA_ACCESS_TOKEN": ("value",)},
        ),
        # mit-learn's Production Keycloak client for this environment
        # (opik-mit-learn-<env>) for ol-llm's embedding traces, which the
        # Production keycloak substructure writes to this environment's Vault.
        VaultSecret(
            name=OPIK_SECRET,
            kind="static",
            mount=OPIK_SDK_VAULT_MOUNT,
            mount_type="kv-v1",
            path=opik_sdk_vault_path("mit-learn"),
            env={
                name: Template(text)
                for name, text in OPIK_KEYCLOAK_SECRET_TEMPLATES.items()
            },
        ),
        *memory,
        VaultSecret(
            name="secret-global-mailgun",
            kind="static",
            mount="secret-global",
            mount_type="kv-v2",
            path="mailgun",
            env={"MAILGUN_KEY": ("api_key",)},
        ),
        VaultSecret(
            name="secret-global-hmac-token",
            kind="static",
            mount="secret-global",
            mount_type="kv-v2",
            path="shared_hmac",
            env={
                "WEBHOOK_SECRET": Template('{{ get (get .Secrets "learn") "token" }}')
            },
        ),
        # HubSpot API token shared with xPRO for the CRM integration.
        VaultSecret(
            name="xpro-hubspot",
            kind="static",
            mount="secret-xpro",
            mount_type="kv-v1",
            path="hubspot",
            env={"MITOL_HUBSPOT_API_PRIVATE_TOKEN": ("api_private_token",)},
        ),
        VaultSecret(
            name="aws-secrets",
            kind="dynamic",
            mount="aws-mitx",
            path="creds/ol-mitlearn-application",
            env={
                "AWS_ACCESS_KEY_ID": ("access_key",),
                "AWS_SECRET_ACCESS_KEY": ("secret_key",),
            },
        ),
        VaultSecret(
            name="psql-secrets",
            kind="dynamic",
            mount=database_mount,
            path="creds/app",
            env={
                "DATABASE_URL": Template(
                    'postgres://{{ get .Secrets "username" }}'
                    ':{{ get .Secrets "password" }}'
                    f"@{database_host}/mitopen"
                ),
            },
        ),
        *starrocks,
    ]


def secret_env_names(*, starrocks_mount: str | None, memory_enabled: bool) -> list[str]:
    """Return the variables ``vault_secrets`` writes.

    The other arguments only reach mounts and template text, so empty ones
    yield the same names. ``starrocks_mount`` and ``memory_enabled`` decide
    whether two secrets exist, so they come from the caller.
    """
    return env_names(
        vault_secrets(
            mitlearn_mount="",
            database_mount="",
            database_host="",
            starrocks_mount=starrocks_mount,
            memory_enabled=memory_enabled,
        )
    )
