"""Tests for learn-ai's Vault-backed variable description."""

from pathlib import Path

import yaml

from ol_infrastructure.applications.learn_ai.vault_secrets import (
    secret_env_names,
    secret_named,
    undescribed_static_keys,
    vault_secrets,
)
from ol_infrastructure.lib.opik import OPIK_KEYCLOAK_SECRET_TEMPLATES

SOPS_DIR = Path(__file__).parents[4] / "src/bridge/secrets/learn_ai"
# The keys the deployed program adds beside the sops files' before writing Vault.
# The test cannot see the program: a key it starts adding fails the program's own
# check, while one it stops adding stays here until someone removes it.
PROGRAM_ADDED_KEYS = {
    "POSTHOG_PERSONAL_API_KEY",
    "POSTHOG_PROJECT_API_KEY",
    "SENTRY_DSN",
}
SECRETS = vault_secrets(
    database_mount="postgres-learn-ai",
    database_host="db.example:5432",
    database_name="learnai",
)


def sops_keys(env: str) -> set[str]:
    # sops encrypts values and leaves keys readable, so the file's keys are the
    # variables the program writes to Vault for that stack.
    data = yaml.safe_load((SOPS_DIR / f"secrets.{env}.yaml").read_text())
    return {key for key in data if key != "sops"}


def test_the_static_secret_describes_exactly_what_the_stacks_write():
    written = set().union(*(sops_keys(env) for env in ("ci", "qa", "production")))
    static = secret_named(SECRETS, "learn-ai-secrets")
    assert set(static.env) == written | PROGRAM_ADDED_KEYS
    assert all(static.env[name] == (name,) for name in static.env)


def test_undescribed_static_keys_names_the_extras_only():
    assert undescribed_static_keys(["SECRET_KEY", "NEW_THING", "ALSO_NEW"]) == [
        "ALSO_NEW",
        "NEW_THING",
    ]


def test_the_database_url_template_carries_host_port_and_database():
    assert secret_named(SECRETS, "learn-ai-db-creds").templates == {
        "DATABASE_URL": (
            'postgres://{{ get .Secrets "username"}}:{{ get .Secrets "password" }}'
            "@db.example:5432/learnai"
        ),
    }


def test_the_opik_templates_are_the_shared_ones():
    assert (
        secret_named(SECRETS, "learn-ai-opik-keycloak").templates
        == OPIK_KEYCLOAK_SECRET_TEMPLATES
    )


def test_secret_env_names_is_database_url_then_static_then_opik():
    assert secret_env_names() == [
        "DATABASE_URL",
        *secret_named(SECRETS, "learn-ai-secrets").env,
        *OPIK_KEYCLOAK_SECRET_TEMPLATES,
    ]
