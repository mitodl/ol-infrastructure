# ruff: noqa: E501
"""MITx Online's Vault-backed environment variables.

The deployed program creates VSO resources from this (k8s_secrets.py); the
local-dev program builds its Secret's keys from ``secret_env_names``.
"""

from pulumi import Output

from ol_infrastructure.lib.secret_env import Template, VaultSecret, env_names


def vault_secrets(
    *,
    database_mount: str | Output[str],
    database_host: str,
    openedx_environment: str,
) -> list[VaultSecret]:
    """Describe every Vault path the app reads and the variables it supplies.

    :param database_mount: The Vault database engine's mount.
    :param database_host: ``host:port`` of the app's database.
    :param openedx_environment: e.g. ``mitxonline-qa``; prefixes the
        registration token's path.
    """
    return [
        VaultSecret(
            name="aws-mitxonline",
            kind="dynamic",
            mount="aws-mitx",
            path="creds/mitxonline",
            env={
                "AWS_ACCESS_KEY_ID": ("access_key",),
                "AWS_SECRET_ACCESS_KEY": ("secret_key",),
            },
        ),
        VaultSecret(
            name="postgres-mitxonline",
            kind="dynamic",
            mount=database_mount,
            path="creds/app",
            env={
                "DATABASE_URL": Template(
                    f'postgres://{{{{ get .Secrets "username" }}}}:{{{{ get .Secrets "password" }}}}@{database_host}/mitxonline'
                ),
            },
        ),
        VaultSecret(
            name="mitxonline-collected",
            kind="static",
            mount="secret-mitxonline",
            mount_type="kv-v1",
            path="collected-static-secrets",
            env={
                "MITX_ONLINE_FASTLY_API_KEY": ("fastly", "api_key"),
                "MITX_ONLINE_FASTLY_AUTH_TOKEN": ("fastly", "api_key"),
                "MITX_ONLINE_FASTLY_SERVICE_ID": ("fastly", "service_id"),
                "MIT_LEARN_FASTLY_SERVICE_ID": ("fastly", "mit_learn_service_id"),
                "HUBSPOT_HOME_PAGE_FORM_GUID": ("hubspot", "formId"),
                "HUBSPOT_PORTAL_ID": ("hubspot", "portalId"),
                "MITOL_GOOGLE_SHEETS_DRIVE_API_PROJECT_ID": (
                    "google-sheets",
                    "drive-api-project-id",
                ),
                "MITOL_GOOGLE_SHEETS_DRIVE_CLIENT_ID": (
                    "google-sheets",
                    "drive-client-id",
                ),
                "MITOL_GOOGLE_SHEETS_DRIVE_CLIENT_SECRET": (
                    "google-sheets",
                    "drive-client-secret",
                ),
                "MITOL_GOOGLE_SHEETS_ENROLLMENT_CHANGE_SHEET_ID": (
                    "google-sheets",
                    "enrollment-change-sheet-id",
                ),
                "MITOL_HUBSPOT_API_PRIVATE_TOKEN": ("hubspot", "private-api-token"),
                "UAI_MITOL_HUBSPOT_API_PRIVATE_TOKEN": (
                    "hubspot",
                    "uai-private-api-token",
                ),
                "CYBERSOURCE_INQUIRY_LOG_NACL_ENCRYPTION_KEY": (
                    "cybersource",
                    "inquiry-log-nacl-encryption-key",
                ),
                "MITOL_PAYMENT_GATEWAY_CYBERSOURCE_ACCESS_KEY": (
                    "cybersource",
                    "access-key",
                ),
                "MITOL_PAYMENT_GATEWAY_CYBERSOURCE_MERCHANT_ID": (
                    "cybersource",
                    "merchant-id",
                ),
                "MITOL_PAYMENT_GATEWAY_CYBERSOURCE_MERCHANT_SECRET": (
                    "cybersource",
                    "merchant-secret",
                ),
                "MITOL_PAYMENT_GATEWAY_CYBERSOURCE_MERCHANT_SECRET_KEY_ID": (
                    "cybersource",
                    "merchant-secret-key-id",
                ),
                "MITOL_PAYMENT_GATEWAY_CYBERSOURCE_PROFILE_ID": (
                    "cybersource",
                    "profile-id",
                ),
                "MITOL_PAYMENT_GATEWAY_CYBERSOURCE_SECURITY_KEY": (
                    "cybersource",
                    "security-key",
                ),
                "MITOL_PAYMENT_GATEWAY_STRIPE_API_KEY": ("stripe", "api-key"),
                "MITX_ONLINE_REFINE_OIDC_CONFIG_CLIENT_ID": (
                    "refine-oidc",
                    "client-id",
                ),
                "MAILGUN_WEBHOOK_SIGNING_SECRET": ("mailgun", "webhook-signing-secret"),
                "OIDC_RSA_PRIVATE_KEY": ("refine-oidc", "rsa-private-key"),
                "OPENEDX_API_CLIENT_ID": ("open-edx-api-client", "client-id"),
                "OPENEDX_API_CLIENT_SECRET": ("open-edx-api-client", "client-secret"),
                "OPENEDX_COURSES_SERVICE_WORKER_CLIENT_ID": (
                    "open-edx-courses-service-worker",
                    "client-id",
                ),
                "OPENEDX_COURSES_SERVICE_WORKER_CLIENT_SECRET": (
                    "open-edx-courses-service-worker",
                    "client-secret",
                ),
                "OPENEDX_RETIREMENT_SERVICE_WORKER_CLIENT_ID": (
                    "open-edx-retirement-service-worker",
                    "client-id",
                ),
                "OPENEDX_RETIREMENT_SERVICE_WORKER_CLIENT_SECRET": (
                    "open-edx-retirement-service-worker",
                    "client-secret",
                ),
                "OPENEDX_SERVICE_WORKER_API_TOKEN": (
                    "open-edx-service-worker",
                    "api-token",
                ),
                "OPEN_EXCHANGE_RATES_APP_ID": ("open-exchange-rates", "app-id"),
                "POSTHOG_API_TOKEN": ("posthog", "api-token"),
                "POSTHOG_PROJECT_API_KEY": ("posthog", "api-token"),
                "RECAPTCHA_SECRET_KEY": ("recaptcha", "secret-key"),
                "RECAPTCHA_SITE_KEY": ("recaptcha", "site-key"),
                "SECRET_KEY": ("django", "secret-key"),
                "STATUS_TOKEN": ("django", "status-token"),
                "TRINO_CATALOG": Template("ol_data_lake_production"),
                "TRINO_HOST": Template(
                    "mitol-ol-data-interactive.trino.galaxy.starburst.io"
                ),
                "TRINO_PASSWORD": ("data_platform", "trino_password"),
                "TRINO_PORT": Template("443"),
                "TRINO_USER": ("data_platform", "trino_username"),
            },
        ),
        VaultSecret(
            name="mitxonline-mitxonline-registration-access-token",
            kind="static",
            mount="secret-mitxonline",
            mount_type="kv-v1",
            path=f"{openedx_environment}/mitxonline-registration-access-token",
            env={"MITX_ONLINE_REGISTRATION_ACCESS_TOKEN": ("value",)},
        ),
        VaultSecret(
            name="mitxonline-keycloak-scim-details",
            kind="static",
            mount="secret-mitxonline",
            mount_type="kv-v1",
            path="keycloak-scim",
            env={
                "MITOL_SCIM_KEYCLOAK_CLIENT_ID": ("client_id",),
                "MITOL_SCIM_KEYCLOAK_CLIENT_SECRET": ("client_secret",),
            },
        ),
        VaultSecret(
            name="mitxonline-keycloak-admin-b2b",
            kind="static",
            mount="secret-mitxonline",
            mount_type="kv-v1",
            path="keycloak-admin-b2b",
            env={
                "KEYCLOAK_DISCOVERY_URL": Template(
                    '{{ get .Secrets "url" }}/.well-known/openid-configuration'
                ),
                "KEYCLOAK_ADMIN_CLIENT_ID": ("client_id",),
                "KEYCLOAK_ADMIN_CLIENT_SECRET": ("client_secret",),
                "KEYCLOAK_ADMIN_CLIENT_SCOPES": Template("openid profile email"),
                "KEYCLOAK_ADMIN_CLIENT_NO_VERIFY_SSL": Template("False"),
            },
        ),
        VaultSecret(
            name="global-mailgun",
            kind="static",
            mount="secret-global",
            mount_type="kv-v2",
            path="mailgun",
            env={"MAILGUN_KEY": ("api_key",)},
        ),
        VaultSecret(
            name="operations-mitxonline-sentry-dsn",
            kind="static",
            mount="secret-operations",
            mount_type="kv-v1",
            path="global/mitxonline/sentry-dsn",
            env={"SENTRY_DSN": ("value",)},
        ),
        VaultSecret(
            name="digital-credentials-issuer-coordinator",
            kind="static",
            mount="secret-digital-credentials",
            mount_type="kv-v2",
            path="issuer-coordinator",
            env={
                "VERIFIABLE_CREDENTIAL_BEARER_TOKEN": Template(
                    '{{ get (get .Secrets "tenant_tokens") "TENANT_TOKEN_DEFAULT" }}'
                ),
            },
        ),
        VaultSecret(
            name="digital-credentials-signing-service",
            kind="static",
            mount="secret-digital-credentials",
            mount_type="kv-v2",
            path="signing-service",
            env={
                "VERIFIABLE_CREDENTIAL_DID": Template(
                    '{{ $json := get (get .Secrets "tenants_did_keys") "DEFAULT" | fromJson }}{{ get (get $json "keyPair") "publicKeyMultibase" }}'
                ),
            },
        ),
    ]


def secret_env_names() -> list[str]:
    """Return the variables ``vault_secrets`` writes.

    The arguments only reach mounts, paths and template text, so empty ones
    yield the same names. That holds while no secret depends on an argument;
    once one is conditional (mit_learn's StarRocks credentials exist only when
    a mount is set), the argument that decides it has to come from the caller.
    """
    return env_names(
        vault_secrets(database_mount="", database_host="", openedx_environment="")
    )
