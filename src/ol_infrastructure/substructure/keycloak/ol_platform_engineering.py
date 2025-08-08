"""Keycloak realm definition for OL Platform Engineering."""

import json

import pulumi_keycloak as keycloak
import pulumi_vault as vault
from pulumi import Config, Output, ResourceOptions


def create_ol_platform_engineering_realm(
    keycloak_provider: keycloak.Provider,
    keycloak_url: str,
    env_name: str,
    mit_email_password: str,
    mit_email_username: str,
    mit_email_host: str,
    session_secret: str,
    fetch_realm_public_key_partial,
):
    """Create the OL Platform Engineering realm and all of its resources."""
    resource_options = ResourceOptions(provider=keycloak_provider)
    keycloak_realm_config = Config("keycloak_realm")
    ol_platform_engineering_realm = keycloak.Realm(
        "ol-platform-engineering",
        access_code_lifespan="30m",
        access_code_lifespan_user_action="15m",
        attributes={
            "business_unit": f"operations-{env_name}",
        },
        display_name="OL Platform Engineering",
        display_name_html="<b>OL PLatform Engineering</b>",
        enabled=True,
        login_theme="keycloak",
        duplicate_emails_allowed=False,
        otp_policy=keycloak.RealmOtpPolicyArgs(
            algorithm="HmacSHA1",
            digits=6,
            initial_counter=2,
            look_ahead_window=1,
            period=30,
            type="totp",
        ),
        realm="ol-platform-engineering",
        reset_password_allowed=True,
        verify_email=True,
        password_policy=(  # noqa: S106 # pragma: allowlist secret
            "length(30) and forceExpiredPasswordChange(365)  and notUsername and notEmail"
        ),
        security_defenses=keycloak.RealmSecurityDefensesArgs(
            brute_force_detection=keycloak.RealmSecurityDefensesBruteForceDetectionArgs(
                failure_reset_time_seconds=43200,
                max_failure_wait_seconds=900,
                max_login_failures=20,
                minimum_quick_login_wait_seconds=60,
                permanent_lockout=False,
                quick_login_check_milli_seconds=1000,
                wait_increment_seconds=60,
            ),
            headers=keycloak.RealmSecurityDefensesHeadersArgs(
                content_security_policy=(
                    "frame-src 'self'; frame-ancestors 'self'; object-src 'none';"
                ),
                content_security_policy_report_only="",
                strict_transport_security="max-age=31536000; includeSubDomains",
                x_content_type_options="nosniff",
                x_frame_options="DENY",
                x_robots_tag="none",
                x_xss_protection="1; mode=block",
            ),
        ),
        smtp_server=keycloak.RealmSmtpServerArgs(
            auth=keycloak.RealmSmtpServerAuthArgs(
                password=mit_email_password,
                username=mit_email_username,
            ),
            from_="odl-devops@mit.edu",
            from_display_name="Identity - OL Platform Engineering",
            host=mit_email_host,
            port="587",
            reply_to="odl-devops@mit.edu",
            reply_to_display_name="Identity - OL Platform Engineering",
            starttls=True,
        ),
        ssl_required="external",
        offline_session_idle_timeout="168h",
        sso_session_idle_timeout="2h",
        sso_session_max_lifespan="24h",
        opts=resource_options,
    )

    keycloak.RequiredAction(
        "configure-totp",
        realm_id=ol_platform_engineering_realm.realm,
        alias="CONFIGURE_TOTP",
        default_action=True,
        enabled=True,
        opts=resource_options,
    )

    keycloak.RequiredAction(
        "verify_email",
        realm_id=ol_platform_engineering_realm.realm,
        alias="VERIFY_EMAIL",
        default_action=True,
        enabled=True,
        opts=resource_options,
    )

    keycloak.RequiredAction(
        "update_password",
        realm_id=ol_platform_engineering_realm.realm,
        alias="UPDATE_PASSWORD",
        default_action=True,
        enabled=True,
        opts=resource_options,
    )

    # AIRBYTE [START] # noqa: ERA001
    ol_platform_engineering_airbyte_client = keycloak.openid.Client(
        "ol-platform-engineering-airbyte-client",
        name="ol-platform-engineering-airbyte-client",
        realm_id="ol-platform-engineering",
        client_id="ol-airbyte-client",
        client_secret=keycloak_realm_config.get(
            "ol-platform-engineering-airbyte-client-secret"
        ),
        enabled=True,
        access_type="CONFIDENTIAL",
        standard_flow_enabled=True,
        implicit_flow_enabled=False,
        service_accounts_enabled=False,
        valid_redirect_uris=keycloak_realm_config.get_object(
            "ol-platform-engineering-airbyte-redirect-uris"
        ),
        opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
    )
    vault.generic.Secret(
        "ol-platform-engineering-airbyte-client-vault-oidc-credentials",
        path="secret-operations/sso/airbyte",
        data_json=Output.all(
            url=ol_platform_engineering_airbyte_client.realm_id.apply(
                lambda realm_id: f"{keycloak_url}/realms/{realm_id}"
            ),
            client_id=ol_platform_engineering_airbyte_client.client_id,
            client_secret=ol_platform_engineering_airbyte_client.client_secret,
            # This is included for the case where we are using traefik-forward-auth.
            # It requires a random secret value to be present which is independent
            # of the OAuth credentials.
            secret=session_secret,
            realm_id=ol_platform_engineering_airbyte_client.realm_id,
            realm_name="ol-platform-engineering",
            realm_public_key=ol_platform_engineering_airbyte_client.realm_id.apply(
                lambda realm_id: fetch_realm_public_key_partial(realm_id)
            ),
        ).apply(json.dumps),
    )
    # AIRBYTE [END] # noqa: ERA001

    # DAGSTER [START] # noqa: ERA001
    ol_platform_engineering_dagster_client = keycloak.openid.Client(
        "ol-platform-engineering-dagster-client",
        name="ol-platform-engineering-dagster-client",
        realm_id="ol-platform-engineering",
        client_id="ol-dagster-client",
        client_secret=keycloak_realm_config.get(
            "ol-platform-engineering-dagster-client-secret"
        ),
        enabled=True,
        access_type="CONFIDENTIAL",
        standard_flow_enabled=True,
        implicit_flow_enabled=False,
        service_accounts_enabled=False,
        valid_redirect_uris=keycloak_realm_config.get_object(
            "ol-platform-engineering-dagster-redirect-uris"
        ),
        opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
    )
    vault.generic.Secret(
        "ol-platform-engineering-dagster-client-vault-oidc-credentials",
        path="secret-operations/sso/dagster",
        data_json=Output.all(
            url=ol_platform_engineering_dagster_client.realm_id.apply(
                lambda realm_id: f"{keycloak_url}/realms/{realm_id}"
            ),
            client_id=ol_platform_engineering_dagster_client.client_id,
            client_secret=ol_platform_engineering_dagster_client.client_secret,
            # This is included for the case where we are using traefik-forward-auth.
            # It requires a random secret value to be present which is independent
            # of the OAuth credentials.
            secret=session_secret,
            realm_id=ol_platform_engineering_dagster_client.realm_id,
            realm_name="ol-platform-engineering",
            realm_public_key=ol_platform_engineering_dagster_client.realm_id.apply(
                lambda realm_id: fetch_realm_public_key_partial(realm_id)
            ),
        ).apply(json.dumps),
    )
    # DAGSTER [END] # noqa: ERA001

    # LEEK [START] # noqa: ERA001
    ol_platform_engineering_leek_client = keycloak.openid.Client(
        "ol-platform-engineering-leek-client",
        name="ol-platform-engineering-leek-client",
        realm_id="ol-platform-engineering",
        client_id="ol-leek-client",
        client_secret=keycloak_realm_config.get(
            "ol-platform-engineering-leek-client-secret"
        ),
        enabled=True,
        access_type="CONFIDENTIAL",
        standard_flow_enabled=True,
        implicit_flow_enabled=False,
        service_accounts_enabled=False,
        valid_redirect_uris=keycloak_realm_config.get_object(
            "ol-platform-engineering-leek-redirect-uris"
        ),
        opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
    )
    vault.generic.Secret(
        "ol-platform-engineering-leek-client-vault-oidc-credentials",
        path="secret-operations/sso/leek",
        data_json=Output.all(
            url=ol_platform_engineering_leek_client.realm_id.apply(
                lambda realm_id: f"{keycloak_url}/realms/{realm_id}"
            ),
            client_id=ol_platform_engineering_leek_client.client_id,
            client_secret=ol_platform_engineering_leek_client.client_secret,
            # This is included for the case where we are using traefik-forward-auth.
            # It requires a random secret value to be present which is independent
            # of the OAuth credentials.
            secret=session_secret,
            realm_id=ol_platform_engineering_leek_client.realm_id,
            realm_name="ol-platform-engineering",
            realm_public_key=ol_platform_engineering_leek_client.realm_id.apply(
                lambda realm_id: fetch_realm_public_key_partial(realm_id)
            ),
        ).apply(json.dumps),
    )
    # LEEK [END] # noqa: ERA001

    # VAULT [START] # noqa: ERA001
    if keycloak_realm_config.get("ol-platform-engineering-vault-client-secret"):
        ol_platform_engineering_vault_client = keycloak.openid.Client(
            "ol-platform-engineering-vault-client",
            name="ol-platform-engineering-vault-client",
            realm_id="ol-platform-engineering",
            client_id="ol-vault-client",
            client_secret=keycloak_realm_config.get(
                "ol-platform-engineering-vault-client-secret"
            ),
            enabled=True,
            access_type="CONFIDENTIAL",
            standard_flow_enabled=True,
            implicit_flow_enabled=False,
            service_accounts_enabled=False,
            valid_redirect_uris=keycloak_realm_config.get_object(
                "ol-platform-engineering-vault-redirect-uris"
            ),
            opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
        )
        vault.generic.Secret(
            "ol-platform-engineering-vault-client-vault-oidc-credentials",
            path="secret-operations/sso/vault",
            data_json=Output.all(
                url=ol_platform_engineering_vault_client.realm_id.apply(
                    lambda realm_id: f"{keycloak_url}/realms/{realm_id}"
                ),
                client_id=ol_platform_engineering_vault_client.client_id,
                client_secret=ol_platform_engineering_vault_client.client_secret,
                # This is included for the case where we are using traefik-forward-auth.
                # It requires a random secret value to be present which is independent
                # of the OAuth credentials.
                secret=session_secret,
                realm_id=ol_platform_engineering_vault_client.realm_id,
                realm_name="ol-platform-engineering",
                realm_public_key=ol_platform_engineering_vault_client.realm_id.apply(
                    lambda realm_id: fetch_realm_public_key_partial(realm_id)
                ),
            ).apply(json.dumps),
        )
    # VAULT [END] # noqa: ERA001
    return ol_platform_engineering_realm
