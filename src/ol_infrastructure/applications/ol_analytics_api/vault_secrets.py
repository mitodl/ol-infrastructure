"""ol-analytics-api's Vault-backed environment variables.

The deployed program takes each secret's mount, path and VSO templates from
this description; the local-dev program builds its Secret's keys from
``secret_env_names``. ``ol-analytics-api-static-secrets`` syncs every key of its
path (``includes=["*"]``) rather than a template per variable, so for it the
description lists what the program writes there. The StarRocks credentials are
not here: the app reads those from Vault itself (see
``definition.StarRocksVaultLogin``).
"""

from ol_infrastructure.lib.secret_env import VaultSecret, env_names

MITXONLINE_CLIENT_ID = "OL_ANALYTICS_API_B2B_DASHBOARD_MITXONLINE_CLIENT_ID"
MITXONLINE_CLIENT_SECRET = "OL_ANALYTICS_API_B2B_DASHBOARD_MITXONLINE_CLIENT_SECRET"  # noqa: S105  # pragma: allowlist secret


def vault_secrets() -> list[VaultSecret]:
    """Describe every Vault path the app's pods read and the variables each supplies."""
    return [
        # Written by the deployed program from the Sentry stack's DSN.
        VaultSecret(
            name="ol-analytics-api-static-secrets",
            kind="static",
            mount="secret-ol-analytics-api",
            mount_type="kv-v2",
            path="secrets",
            env={"SENTRY_DSN": ("SENTRY_DSN",)},
        ),
        # The OAuth2 client-credentials pair the org-manager check presents to
        # MITx Online (mitodl/mitxonline#3807).
        VaultSecret(
            name="ol-analytics-api-mitxonline-oauth",
            kind="static",
            mount="secret-operations",
            mount_type="kv-v1",
            path="ol-analytics-api/mitxonline-oauth",
            env={
                MITXONLINE_CLIENT_ID: ("client_id",),
                MITXONLINE_CLIENT_SECRET: ("client_secret",),
            },
        ),
        # The realm the learner-records tenant verifies partner tokens against,
        # and the audience it requires: the canonical host's Keycloak client,
        # which the route's openid-connect plugin reads too, so the gateway and
        # the app cannot check different realms or audiences.
        VaultSecret(
            name="ol-analytics-api-learner-records-oidc",
            kind="static",
            mount="secret-operations",
            mount_type="kv-v1",
            path="sso/ol-analytics-api",
            env={
                "OL_ANALYTICS_API_B2B_LEARNER_RECORDS_ISSUER": ("url",),
                "OL_ANALYTICS_API_B2B_LEARNER_RECORDS_AUDIENCE": ("client_id",),
            },
        ),
    ]


def vault_keys(secret: VaultSecret) -> list[str]:
    """Return the top-level keys of the Vault entry ``secret`` reads, in order.

    :raises ValueError: If a variable comes from template text, whose keys this
        cannot see.
    """
    keys = []
    for name, source in secret.env.items():
        if not isinstance(source, tuple):
            msg = f"{secret.name}: {name} is template text; list its keys by hand"
            raise ValueError(msg)  # noqa: TRY004
        keys.append(source[0])
    return keys


def secret_env_names() -> list[str]:
    """Return the variables ``vault_secrets`` writes."""
    return env_names(vault_secrets())
