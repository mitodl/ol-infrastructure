"""learn-ai's Vault-backed environment variables.

The deployed program takes the database and Opik secrets' VSO templates from
this description, writes the keys of ``secret-learn-ai/secrets`` to Vault itself
from the sops file and refuses one this description lacks; the local-dev
program builds its Secret's keys from ``secret_env_names``. That static secret
still syncs every key of its Vault path (``includes=["*"]``) rather than a
template per variable, so for it the description is checked, not rendered.
"""

from collections.abc import Iterable

from pulumi import Output

from ol_infrastructure.lib.opik import (
    OPIK_KEYCLOAK_SECRET_TEMPLATES,
    OPIK_SDK_VAULT_MOUNT,
    opik_sdk_vault_path,
)
from ol_infrastructure.lib.secret_env import Template, VaultSecret, env_names


def vault_secrets(
    *,
    database_mount: str | Output[str],
    database_host: str,
    database_name: str,
) -> list[VaultSecret]:
    """Describe every Vault path the app reads and the variables it supplies.

    :param database_mount: The Vault database engine's mount.
    :param database_host: ``host:port`` of the app's database.
    :param database_name: The app's database.
    """
    return [
        VaultSecret(
            name="learn-ai-db-creds",
            kind="dynamic",
            mount=database_mount,
            path="creds/app",
            env={
                "DATABASE_URL": Template(
                    'postgres://{{ get .Secrets "username"}}'
                    ':{{ get .Secrets "password" }}'
                    f"@{database_host}/{database_name}"
                ),
            },
        ),
        # The union of every stack's secrets.<env>.yaml plus the PostHog keys and
        # Sentry DSN the program adds; a stack may write a subset. Each key is the
        # variable's own name, because the program writes the file's keys as is.
        VaultSecret(
            name="learn-ai-secrets",
            kind="static",
            mount="secret-learn-ai",
            mount_type="kv-v2",
            path="secrets",
            env={
                "AI_ZENDESK_OAUTH_CLIENT_ID": ("AI_ZENDESK_OAUTH_CLIENT_ID",),
                "AI_ZENDESK_OAUTH_CLIENT_SECRET": ("AI_ZENDESK_OAUTH_CLIENT_SECRET",),
                "GEMINI_API_KEY": ("GEMINI_API_KEY",),
                "LANGSMITH_API_KEY": ("LANGSMITH_API_KEY",),
                "LEARN_ACCESS_TOKEN": ("LEARN_ACCESS_TOKEN",),
                "MAILGUN_KEY": ("MAILGUN_KEY",),
                "MAILGUN_RECIPIENT_OVERRIDE": ("MAILGUN_RECIPIENT_OVERRIDE",),
                "MAILGUN_SENDER_DOMAIN": ("MAILGUN_SENDER_DOMAIN",),
                "MAILGUN_URL": ("MAILGUN_URL",),
                "OPENAI_API_KEY": ("OPENAI_API_KEY",),
                "POSTHOG_PERSONAL_API_KEY": ("POSTHOG_PERSONAL_API_KEY",),
                "POSTHOG_PROJECT_API_KEY": ("POSTHOG_PROJECT_API_KEY",),
                "SECRET_KEY": ("SECRET_KEY",),
                "SENTRY_DSN": ("SENTRY_DSN",),
                "STATUS_TOKEN": ("STATUS_TOKEN",),
                "TOGETHERAI_API_KEY": ("TOGETHERAI_API_KEY",),
            },
        ),
        VaultSecret(
            name="learn-ai-opik-keycloak",
            kind="static",
            mount=OPIK_SDK_VAULT_MOUNT,
            mount_type="kv-v1",
            path=opik_sdk_vault_path("learn-ai"),
            env={
                name: Template(text)
                for name, text in OPIK_KEYCLOAK_SECRET_TEMPLATES.items()
            },
        ),
    ]


def secret_named(secrets: Iterable[VaultSecret], name: str) -> VaultSecret:
    """Return the secret called ``name``.

    :raises KeyError: If there is none. A bare ``StopIteration`` would surface
        from inside a Pulumi ``apply`` as a ``RuntimeError`` that names nothing.
    """
    secret = next((secret for secret in secrets if secret.name == name), None)
    if secret is None:
        raise KeyError(name)
    return secret


def undescribed_static_keys(keys: Iterable[str]) -> list[str]:
    """Return the keys among ``keys`` that ``learn-ai-secrets`` does not describe."""
    static = secret_named(
        vault_secrets(database_mount="", database_host="", database_name=""),
        "learn-ai-secrets",
    )
    return sorted(set(keys) - set(static.env))


def secret_env_names() -> list[str]:
    """Return the variables ``vault_secrets`` writes.

    The arguments only reach mounts and template text, so empty ones yield the
    same names.
    """
    return env_names(
        vault_secrets(database_mount="", database_host="", database_name="")
    )
