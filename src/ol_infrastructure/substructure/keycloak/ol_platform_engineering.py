"""Keycloak realm definition for OL Platform Engineering."""

import json

import pulumi_keycloak as keycloak
import pulumi_vault as vault
from pulumi import Config, Output, ResourceOptions


def create_ol_platform_engineering_realm(  # noqa: PLR0913
    keycloak_provider: keycloak.Provider,
    keycloak_url: str,
    env_name: str,
    stack_info,
    mit_email_password: str,
    mit_email_username: str,
    mit_email_host: str,
    session_secret: str,
    fetch_realm_public_key_partial,
):
    """Create the OL Platform Engineering realm and all of its resources."""
    resource_options = ResourceOptions(provider=keycloak_provider)
    keycloak_realm_config = Config("keycloak_realm")
    if stack_info.env_suffix == "production":
        derived_relying_party_id = "sso.ol.mit.edu"
    else:
        derived_relying_party_id = f"sso-{stack_info.env_suffix}.ol.mit.edu"
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
        account_theme="keycloak.v3",
        admin_theme="keycloak.v2",
        login_theme="keycloak.v2",
        email_theme="keycloak",
        registration_email_as_username=True,
        login_with_email_allowed=True,
        duplicate_emails_allowed=False,
        realm="ol-platform-engineering",
        reset_password_allowed=False,
        verify_email=False,
        web_authn_passwordless_policy={
            "relying_party_entity_name": f"mit-ol-sso-{stack_info.env_suffix}",
            "relying_party_id": derived_relying_party_id,
            "require_resident_key": "Yes",
            "user_verification_requirement": "required",
        },
        password_policy=(  # noqa: S106 # pragma: allowlist secret
            "length(12) and upperCase(1) and lowerCase(1) and digits(1) and "
            "specialChars(1) and notUsername and notEmail and passwordHistory(5) "
            "and forceExpiredPasswordChange(120)"
        ),
        registration_allowed=False,
        security_defenses=keycloak.RealmSecurityDefensesArgs(
            brute_force_detection=keycloak.RealmSecurityDefensesBruteForceDetectionArgs(
                failure_reset_time_seconds=43200,
                max_failure_wait_seconds=900,
                max_login_failures=10,
                minimum_quick_login_wait_seconds=60,
                permanent_lockout=True,
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

    keycloak.RealmEvents(
        "ol-platform-engineering-realm-events",
        realm_id=ol_platform_engineering_realm.realm,
        events_enabled=True,
        events_expiration=604800,
        admin_events_enabled=True,
        admin_events_details_enabled=True,
        enabled_event_types=[],
        events_listeners=["jboss-logging"],
    )

    keycloak.RequiredAction(
        "ol-platform-engineering-verify_email",
        realm_id=ol_platform_engineering_realm.realm,
        alias="VERIFY_EMAIL",
        default_action=True,
        enabled=True,
        opts=resource_options,
    )

    # Create realm roles for ol-platform-engineering
    keycloak.Role(
        "ol-platform-engineering-admin-role",
        realm_id=ol_platform_engineering_realm.id,
        name="admin",
        description="OL Platform Engineering Admin role",
        opts=resource_options,
    )

    keycloak.Role(
        "ol-platform-engineering-developer-role",
        realm_id=ol_platform_engineering_realm.id,
        name="developer",
        description="OL Platform Engineering Developer role",
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

    # OL Platform Engineering Realm - Authentication Flows[START]
    # OL - browser flow [START]
    # username-form -> ol-auth-username-password-form

    ol_browser_platform_engineering_flow = keycloak.authentication.Flow(
        "ol-browser-platform-engineering-flow",
        realm_id=ol_platform_engineering_realm.id,
        alias="ol-browser-data-platform-flow",
        opts=resource_options,
    )
    keycloak.authentication.Execution(
        "ol-browser-platform-engineering-auth-cookie",
        realm_id=ol_platform_engineering_realm.id,
        parent_flow_alias=ol_browser_platform_engineering_flow.alias,
        authenticator="auth-cookie",
        requirement="ALTERNATIVE",
        priority=10,
        opts=resource_options,
    )
    keycloak.authentication.Execution(
        "ol-browser-platform-engineering-idp-redirector",
        realm_id=ol_platform_engineering_realm.id,
        parent_flow_alias=ol_browser_platform_engineering_flow.alias,
        authenticator="identity-provider-redirector",
        requirement="ALTERNATIVE",
        priority=20,
        opts=resource_options,
    )
    ol_browser_platform_engineering_flow_org = keycloak.authentication.Subflow(
        "ol-browser-platform-engineering-flow-org",
        realm_id=ol_platform_engineering_realm.id,
        alias="ol-browser-data-platform-flow-org",
        parent_flow_alias=ol_browser_platform_engineering_flow.alias,
        provider_id="basic-flow",
        requirement="ALTERNATIVE",
        priority=30,
        opts=resource_options,
    )
    keycloak.authentication.Execution(
        "ol-browser-platform-engineering-flow-org-user-configured",
        realm_id=ol_platform_engineering_realm.id,
        parent_flow_alias=ol_browser_platform_engineering_flow_org.alias,
        authenticator="conditional-user-configured",
        priority=40,
        requirement="REQUIRED",
        opts=resource_options,
    )
    keycloak.authentication.Execution(
        "ol-browser-platform-engineering-flow-org-identity-first",
        realm_id=ol_platform_engineering_realm.id,
        parent_flow_alias=ol_browser_platform_engineering_flow_org.alias,
        authenticator="organization",
        priority=50,
        requirement="ALTERNATIVE",
        opts=resource_options,
    )
    ol_browser_platform_engineering_passkey_flow = keycloak.authentication.Subflow(
        "ol-browser-platform-engineering-passkey-flow",
        realm_id=ol_platform_engineering_realm.id,
        alias="ol-browser-platform-engineering-passkey-flow",
        parent_flow_alias=ol_browser_platform_engineering_flow.alias,
        provider_id="basic-flow",
        priority=60,
        requirement="REQUIRED",
        opts=resource_options,
    )
    keycloak.authentication.Execution(
        "ol-browser-platform-engineering-flow-username-form",
        realm_id=ol_platform_engineering_realm.id,
        parent_flow_alias=ol_browser_platform_engineering_passkey_flow.alias,
        authenticator="auth-username-form",
        requirement="REQUIRED",
        priority=70,
        opts=resource_options,
    )
    keycloak.authentication.Execution(
        "ol-browser-platform-engineering-webauthn-authenticator-flow",
        realm_id=ol_platform_engineering_realm.id,
        parent_flow_alias=ol_browser_platform_engineering_passkey_flow.alias,
        authenticator="webauthn-authenticator-passwordless",
        requirement="REQUIRED",
        priority=80,
        opts=resource_options,
    )
    # Bind the flow to the ol-platform-engineering realm for browser login.
    keycloak.authentication.Bindings(
        "ol-platform-engineering-browser-authentication-binding",
        realm_id=ol_platform_engineering_realm.id,
        browser_flow=ol_browser_platform_engineering_flow.alias,
        opts=resource_options,
    )
    # OL Platform Engineering - browser flow [END]
    # OL Platform Engineering Realm - Authentication Flows[END]
    return ol_platform_engineering_realm
