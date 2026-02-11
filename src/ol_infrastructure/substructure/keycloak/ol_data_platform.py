"""Keycloak realm definition for the OL Data Platform."""

import json

import pulumi_keycloak as keycloak
import pulumi_vault as vault
from pulumi import Config, InvokeOptions, Output, ResourceOptions


def create_ol_data_platform_realm(  # noqa: PLR0913, PLR0915
    keycloak_provider: keycloak.Provider,
    keycloak_url: str,
    env_name: str,
    stack_info,
    mit_email_password: str,
    mit_email_username: str,
    mit_email_host: str,
    mit_touchstone_cert: str,
    session_secret: str,
    fetch_realm_public_key_partial,
):
    """Create the OL Data Platform realm and all of its resources."""
    resource_options = ResourceOptions(provider=keycloak_provider)
    keycloak_realm_config = Config("keycloak_realm")
    if stack_info.env_suffix == "production":
        derived_relying_party_id = "sso.ol.mit.edu"
    else:
        derived_relying_party_id = f"sso-{stack_info.env_suffix}.ol.mit.edu"
    ol_data_platform_realm = keycloak.Realm(
        "ol-data-platform",
        access_code_lifespan="30m",
        access_code_lifespan_user_action="15m",
        attributes={
            "business_unit": f"operations-{env_name}",
        },
        display_name="OL Data",
        display_name_html="<b>OL Data</b>",
        enabled=True,
        account_theme="keycloak.v3",
        admin_theme="keycloak.v2",
        login_theme="keycloak.v2",
        email_theme="ol-data-platform",
        registration_email_as_username=True,
        login_with_email_allowed=True,
        duplicate_emails_allowed=False,
        realm="ol-data-platform",
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
            from_display_name="Identity - OL Data",
            host=mit_email_host,
            port="587",
            reply_to="odl-devops@mit.edu",
            reply_to_display_name="Identity - OL Data",
            starttls=True,
        ),
        ssl_required="external",
        offline_session_idle_timeout="168h",
        organizations_enabled=True,
        sso_session_idle_timeout="2h",
        sso_session_max_lifespan="24h",
        opts=resource_options,
    )

    keycloak.RealmEvents(
        "ol-data-platform-realm-events",
        realm_id=ol_data_platform_realm.realm,
        events_enabled=True,
        events_expiration=604800,
        admin_events_enabled=True,
        admin_events_details_enabled=True,
        enabled_event_types=[],
        events_listeners=["jboss-logging"],
    )

    keycloak.RequiredAction(
        "ol-data-verify-email",
        realm_id=ol_data_platform_realm.realm,
        alias="VERIFY_EMAIL",
        default_action=False,
        enabled=False,
        opts=resource_options,
    )

    # SUPERSET [START] # noqa: ERA001
    ol_data_platform_superset_client = keycloak.openid.Client(
        "ol-data-platform-superset-client",
        name="ol-data-platform-superset-client",
        realm_id=ol_data_platform_realm.id,
        client_id="ol-superset-client",
        client_secret=keycloak_realm_config.get(
            "ol-data-platform-superset-client-secret"
        ),
        enabled=True,
        access_type="CONFIDENTIAL",
        # Needed to use for Superset API access
        direct_access_grants_enabled=True,
        standard_flow_enabled=True,
        implicit_flow_enabled=False,
        service_accounts_enabled=True,
        valid_redirect_uris=keycloak_realm_config.get_object(
            "ol-data-platform-superset-redirect-uris"
        ),
        opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
    )
    ol_data_platform_superset_client_roles = (
        keycloak_realm_config.get_object("ol-data-platform-superset-client-roles") or []
    )
    ol_data_platform_superset_client_role_refs = {}
    for role in ol_data_platform_superset_client_roles:
        role_ref = keycloak.Role(
            f"ol-data-platform-superset-client-{role}",
            name=role,
            realm_id=ol_data_platform_realm.id,
            client_id=ol_data_platform_superset_client.id,
            opts=resource_options,
        )
        ol_data_platform_superset_client_role_refs[role] = role_ref

    vault.generic.Secret(
        "ol-data-platform-superset-client-vault-oidc-credentials",
        path="secret-operations/sso/superset",
        data_json=Output.all(
            url=ol_data_platform_superset_client.realm_id.apply(
                lambda realm_id: f"{keycloak_url}/realms/{realm_id}"
            ),
            client_id=ol_data_platform_superset_client.client_id,
            client_secret=ol_data_platform_superset_client.client_secret,
            # This is included for the case where we are using traefik-forward-auth.
            # It requires a random secret value to be present which is independent
            # of the OAuth credentials.
            secret=session_secret,
            realm_id=ol_data_platform_superset_client.realm_id,
            realm_name="ol-data-platform",
            realm_public_key=ol_data_platform_superset_client.realm_id.apply(
                fetch_realm_public_key_partial
            ),
        ).apply(json.dumps),
    )

    # Provision service account with necessary roles for API operations
    # This enables users calling the Superset API to have their tokens validated
    # and user details looked up via Keycloak admin API
    realm_mgmt_client = keycloak.openid.get_client(
        realm_id="ol-data-platform",
        client_id="realm-management",
        opts=InvokeOptions(provider=keycloak_provider),
    )

    for resource_name, role in [
        ("ol-superset-service-account-view-realm", "view-realm"),
        ("ol-superset-service-account-view-users", "view-users"),
    ]:
        keycloak.openid.ClientServiceAccountRole(
            resource_name,
            realm_id=ol_data_platform_realm.id,
            service_account_user_id=ol_data_platform_superset_client.service_account_user_id,
            client_id=realm_mgmt_client.id,
            role=role,
            opts=resource_options,
        )

    # Create public client for CLI/interactive OAuth flows (no client secret needed)
    # This allows tools like superset-sup to use browser-based auth without secrets
    keycloak.openid.Client(
        "ol-data-platform-superset-cli-client",
        name="ol-data-platform-superset-cli-client",
        realm_id=ol_data_platform_realm.id,
        client_id="ol-superset-cli",
        enabled=True,
        access_type="PUBLIC",  # Public client - no secret required
        standard_flow_enabled=True,  # Authorization code flow
        implicit_flow_enabled=False,
        direct_access_grants_enabled=False,
        service_accounts_enabled=False,
        valid_redirect_uris=[
            "http://localhost:8080/callback",  # CLI callback
            "http://localhost:*/callback",  # Allow any localhost port
            "http://127.0.0.1:8080/callback",
        ],
        web_origins=["+"],  # Allow all origins for CORS (CLI use)
        opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
    )

    # Create realm roles for ol-data-platform
    keycloak.Role(
        "ol-platform-admin-role",
        realm_id=ol_data_platform_realm.id,
        name="ol-platform-admin",
        description=(
            "Full administrative access to all data and platform resources - "
            "maps to superset_admin"
        ),
        composite_roles=[
            ol_data_platform_superset_client_role_refs["ol_platform_admin"].id
        ],
        opts=resource_options,
    )

    keycloak.Role(
        "ol-researcher-role",
        realm_id=ol_data_platform_realm.id,
        name="ol-researcher",
        description=(
            "Research role with ML capabilities and broad data access - "
            "maps to superset_researcher"
        ),
        composite_roles=[
            ol_data_platform_superset_client_role_refs["ol_researcher"].id
        ],
        opts=resource_options,
    )

    keycloak.Role(
        "ol-data-engineer-role",
        realm_id=ol_data_platform_realm.id,
        name="ol-data-engineer",
        description=(
            "Data engineering role with limited production access - "
            "maps to superset_alpha"
        ),
        composite_roles=[
            ol_data_platform_superset_client_role_refs["ol_data_engineer"].id
        ],
        opts=resource_options,
    )

    keycloak.Role(
        "ol-data-analyst-role",
        realm_id=ol_data_platform_realm.id,
        name="ol-data-analyst",
        description=(
            "Data analyst role with read-only access to production data - "
            "maps to superset_gamma"
        ),
        composite_roles=[
            ol_data_platform_superset_client_role_refs["ol_data_analyst"].id
        ],
        opts=resource_options,
    )

    keycloak.Role(
        "ol-instructor",
        realm_id=ol_data_platform_realm.id,
        name="ol-instructor",
        description="Instructor role with limited access to educational data",
        opts=resource_options,
    )

    keycloak.Role(
        "ol-business-analyst",
        realm_id=ol_data_platform_realm.id,
        name="ol-business-analyst",
        description=(
            "Business analyst role similar to existing business_intelligence "
            "and finance roles"
        ),
        opts=resource_options,
    )

    ol_data_platform_role_keys_openid_client_scope = keycloak.openid.ClientScope(
        "ol-data-platform-role-keys-openid-client-scope",
        realm_id=ol_data_platform_realm.id,
        name="ol_roles",
        description="Scope will map a user's group memberships to a claim",
        include_in_token_scope=True,
        opts=resource_options,
    )

    keycloak.openid.UserClientRoleProtocolMapper(
        "ol-data-platform-role-keys-openid-client-scope-mapper",
        claim_name="role_keys",
        realm_id=ol_data_platform_realm.id,
        add_to_access_token=True,
        add_to_id_token=True,
        add_to_userinfo=True,
        claim_value_type="String",
        client_id_for_role_mappings="ol-superset-client",
        client_scope_id=ol_data_platform_role_keys_openid_client_scope.id,
        multivalued=True,
        name="role_keys",
        opts=resource_options,
    )

    keycloak.openid.ClientDefaultScopes(
        "ol-data-platform-superset-client-default-scopes",
        realm_id=ol_data_platform_realm.id,
        client_id=ol_data_platform_superset_client.id,
        default_scopes=[
            "acr",
            "basic",
            "email",
            "ol_roles",
            "openid",
            "profile",
            "roles",
            "web-origins",
        ],
        opts=resource_options,
    )

    # SUPERSET [END] # noqa: ERA001

    # OPENMETADATA [START] # noqa: ERA001
    ol_data_platform_openmetadata_client = keycloak.openid.Client(
        "ol-data-platform-openmetadata-client",
        name="ol-data-platform-openmetadata-client",
        realm_id="ol-data-platform",
        client_id="ol-open_metadata-client",
        client_secret=keycloak_realm_config.get(
            "ol-data-platform-openmetadata-client-secret"
        ),
        enabled=True,
        access_type="CONFIDENTIAL",
        standard_flow_enabled=True,
        implicit_flow_enabled=True,
        service_accounts_enabled=True,
        valid_redirect_uris=keycloak_realm_config.get_object(
            "ol-data-platform-openmetadata-redirect-uris"
        ),
        opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
    )
    ol_data_platform_openmetadata_client_roles = (
        keycloak_realm_config.get_object("ol-data-platform-openmetadata-client-roles")
        or []
    )
    for role in ol_data_platform_openmetadata_client_roles:
        keycloak.Role(
            f"ol-data-platform-openmetadata-client-{role}",
            name=role,
            realm_id="ol-data-platform",
            client_id=ol_data_platform_openmetadata_client.id,
            opts=resource_options,
        )
    vault.generic.Secret(
        "ol-data-platform-openmetadata-client-vault-oidc-credentials",
        path="secret-operations/sso/open_metadata",
        data_json=Output.all(
            url=ol_data_platform_openmetadata_client.realm_id.apply(
                lambda realm_id: f"{keycloak_url}/realms/{realm_id}"
            ),
            client_id=ol_data_platform_openmetadata_client.client_id,
            client_secret=ol_data_platform_openmetadata_client.client_secret,
            # This is included for the case where we are using traefik-forward-auth.
            # It requires a random secret value to be present which is independent
            # of the OAuth credentials.
            secret=session_secret,
            realm_id=ol_data_platform_openmetadata_client.realm_id,
            realm_name="ol-data-platform",
            realm_public_key=ol_data_platform_openmetadata_client.realm_id.apply(
                fetch_realm_public_key_partial
            ),
        ).apply(json.dumps),
    )
    # OPENMETADATA [END] # noqa: ERA001

    # STARROCKS [START] # noqa: ERA001
    ol_data_platform_starrocks_client = keycloak.openid.Client(
        "ol-data-platform-starrocks-client",
        name="ol-data-platform-starrocks-client",
        realm_id=ol_data_platform_realm.id,
        client_id="ol-starrocks-client",
        client_secret=keycloak_realm_config.get(
            "ol-data-platform-starrocks-client-secret"
        ),
        enabled=True,
        access_type="CONFIDENTIAL",
        # Enable direct grants for service account access
        direct_access_grants_enabled=True,
        standard_flow_enabled=True,
        implicit_flow_enabled=False,
        service_accounts_enabled=True,
        valid_redirect_uris=keycloak_realm_config.get_object(
            "ol-data-platform-starrocks-redirect-uris"
        ),
        opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
    )
    ol_data_platform_starrocks_client_roles = (
        keycloak_realm_config.get_object("ol-data-platform-starrocks-client-roles")
        or []
    )
    ol_data_platform_starrocks_client_role_refs = {}
    for role in ol_data_platform_starrocks_client_roles:
        role_ref = keycloak.Role(
            f"ol-data-platform-starrocks-client-{role}",
            name=role,
            realm_id=ol_data_platform_realm.id,
            client_id=ol_data_platform_starrocks_client.id,
            opts=resource_options,
        )
        ol_data_platform_starrocks_client_role_refs[role] = role_ref

    vault.generic.Secret(
        "ol-data-platform-starrocks-client-vault-oidc-credentials",
        path="secret-operations/sso/starrocks",
        data_json=Output.all(
            url=ol_data_platform_starrocks_client.realm_id.apply(
                lambda realm_id: f"{keycloak_url}/realms/{realm_id}"
            ),
            client_id=ol_data_platform_starrocks_client.client_id,
            client_secret=ol_data_platform_starrocks_client.client_secret,
            # This is included for the case where we are using traefik-forward-auth.
            # It requires a random secret value to be present which is independent
            # of the OAuth credentials.
            secret=session_secret,
            realm_id=ol_data_platform_starrocks_client.realm_id,
            realm_name="ol-data-platform",
            realm_public_key=ol_data_platform_starrocks_client.realm_id.apply(
                fetch_realm_public_key_partial
            ),
        ).apply(json.dumps),
    )

    # Provision service account with necessary roles for API operations
    # This enables users calling the StarRocks SQL API to have their tokens validated
    # and user details looked up via Keycloak admin API
    for resource_name, role in [
        ("ol-starrocks-service-account-view-realm", "view-realm"),
        ("ol-starrocks-service-account-view-users", "view-users"),
    ]:
        keycloak.openid.ClientServiceAccountRole(
            resource_name,
            realm_id=ol_data_platform_realm.id,
            service_account_user_id=ol_data_platform_starrocks_client.service_account_user_id,
            client_id=realm_mgmt_client.id,
            role=role,
            opts=resource_options,
        )

    # Map composite realm roles to StarRocks client roles
    keycloak.Role(
        "ol-starrocks-platform-admin-composite",
        realm_id=ol_data_platform_realm.id,
        name="ol-starrocks-admin",
        description="StarRocks administrator with full access",
        composite_roles=[
            ol_data_platform_starrocks_client_role_refs["ol_platform_admin"].id
        ],
        opts=resource_options,
    )

    keycloak.Role(
        "ol-starrocks-data-engineer-composite",
        realm_id=ol_data_platform_realm.id,
        name="ol-starrocks-engineer",
        description="StarRocks data engineer with write access",
        composite_roles=[
            ol_data_platform_starrocks_client_role_refs["ol_data_engineer"].id
        ],
        opts=resource_options,
    )

    keycloak.Role(
        "ol-starrocks-data-analyst-composite",
        realm_id=ol_data_platform_realm.id,
        name="ol-starrocks-analyst",
        description="StarRocks data analyst with read-only access",
        composite_roles=[
            ol_data_platform_starrocks_client_role_refs["ol_data_analyst"].id
        ],
        opts=resource_options,
    )
    # STARROCKS [END] # noqa: ERA001

    # OL Data Platform Realm - Authentication Flows[START]
    # OL - browser flow [START]
    # username-form -> ol-auth-username-password-form

    ol_browser_data_platform_flow = keycloak.authentication.Flow(
        "ol-browser-data-platform-flow",
        realm_id=ol_data_platform_realm.id,
        alias="ol-browser-data-platform-flow",
        opts=resource_options,
    )
    keycloak.authentication.Execution(
        "ol-browser-data-platform-auth-cookie",
        realm_id=ol_data_platform_realm.id,
        parent_flow_alias=ol_browser_data_platform_flow.alias,
        authenticator="auth-cookie",
        requirement="ALTERNATIVE",
        priority=10,
        opts=resource_options,
    )
    keycloak.authentication.Execution(
        "ol-browser-data-platform-idp-redirector",
        realm_id=ol_data_platform_realm.id,
        parent_flow_alias=ol_browser_data_platform_flow.alias,
        authenticator="identity-provider-redirector",
        requirement="ALTERNATIVE",
        priority=20,
        opts=resource_options,
    )
    ol_browser_data_platform_flow_org = keycloak.authentication.Subflow(
        "ol-browser-data-platform-flow-org",
        realm_id=ol_data_platform_realm.id,
        alias="ol-browser-data-platform-flow-org",
        parent_flow_alias=ol_browser_data_platform_flow.alias,
        provider_id="basic-flow",
        requirement="ALTERNATIVE",
        priority=30,
        opts=resource_options,
    )
    keycloak.authentication.Execution(
        "ol-browser-data-platform_flow-org-user-configured",
        realm_id=ol_data_platform_realm.id,
        parent_flow_alias=ol_browser_data_platform_flow_org.alias,
        authenticator="conditional-user-configured",
        priority=40,
        requirement="REQUIRED",
        opts=resource_options,
    )
    keycloak.authentication.Execution(
        "ol-browser-data-platform_flow-org-identity-first",
        realm_id=ol_data_platform_realm.id,
        parent_flow_alias=ol_browser_data_platform_flow_org.alias,
        authenticator="organization",
        priority=50,
        requirement="ALTERNATIVE",
        opts=resource_options,
    )
    ol_browser_data_platform_passkey_flow = keycloak.authentication.Subflow(
        "ol-browser-data-platform-passkey-flow",
        realm_id=ol_data_platform_realm.id,
        alias="ol-browser-data-platform-passkey-flow",
        parent_flow_alias=ol_browser_data_platform_flow.alias,
        provider_id="basic-flow",
        priority=60,
        requirement="REQUIRED",
        opts=resource_options,
    )
    keycloak.authentication.Execution(
        "ol-browser-data-platform-flow-username-form",
        realm_id=ol_data_platform_realm.id,
        parent_flow_alias=ol_browser_data_platform_passkey_flow.alias,
        authenticator="auth-username-form",
        requirement="REQUIRED",
        priority=70,
        opts=resource_options,
    )
    keycloak.authentication.Execution(
        "ol-browser-data-platform-webauthn-authenticator-flow",
        realm_id=ol_data_platform_realm.id,
        parent_flow_alias=ol_browser_data_platform_passkey_flow.alias,
        authenticator="webauthn-authenticator-passwordless",
        requirement="REQUIRED",
        priority=80,
        opts=resource_options,
    )
    # Bind the flow to the ol-data-platform realm for browser login.
    keycloak.authentication.Bindings(
        "ol-data-platform-browser-authentication-binding",
        realm_id=ol_data_platform_realm.id,
        browser_flow=ol_browser_data_platform_flow.alias,
        opts=resource_options,
    )
    # OL Data Platform - browser flow [END]
    # First login flow [START]

    # Does not require email verification or confirmation to connect with existing
    # account.
    ol_data_platform_touchstone_first_login_flow = keycloak.authentication.Flow(
        "ol-data-platform-touchstone-first-login-flow",
        realm_id=ol_data_platform_realm.id,
        alias="ol-data-platform-first-login-flow",
        opts=resource_options,
    )
    ol_data_platform_touchstone_first_login_flow_review_profile = (
        keycloak.authentication.Execution(
            "ol-data-platform-touchstone-first-login-flow-review-profile",
            realm_id=ol_data_platform_realm.id,
            parent_flow_alias=ol_data_platform_touchstone_first_login_flow.alias,
            authenticator="idp-review-profile",
            priority=10,
            requirement="REQUIRED",
            opts=resource_options,
        )
    )
    keycloak.authentication.ExecutionConfig(
        "ol-data-platform-touchstone-first-login-review-profile-config",
        realm_id=ol_data_platform_realm.id,
        execution_id=ol_data_platform_touchstone_first_login_flow_review_profile.id,
        alias="ol-data-platform-review-profile-config",
        config={
            "updateProfileOnFirstLogin": "missing",
        },
        opts=resource_options,
    )
    ol_data_platform_touchstone_user_creation_or_linking_subflow = keycloak.authentication.Subflow(  # noqa: E501
        "ol-data-platform-touchstone-user-creation-or-linking-subflow",
        realm_id=ol_data_platform_realm.id,
        alias="ol-data-platform-touchstone-first-broker-login-user-creation-or-linking",
        parent_flow_alias=ol_data_platform_touchstone_first_login_flow.alias,
        provider_id="basic-flow",
        priority=20,
        requirement="REQUIRED",
        opts=resource_options,
    )
    keycloak.authentication.Execution(
        "ol-data-platform-touchstone-create-user-if-unique",
        realm_id=ol_data_platform_realm.id,
        parent_flow_alias=ol_data_platform_touchstone_user_creation_or_linking_subflow.alias,
        authenticator="idp-create-user-if-unique",
        priority=30,
        requirement="ALTERNATIVE",
        opts=resource_options,
    )
    keycloak.authentication.Execution(
        "ol-data-platform-touchstone-automatically-set-existing-user",
        realm_id=ol_data_platform_realm.id,
        parent_flow_alias=ol_data_platform_touchstone_user_creation_or_linking_subflow.alias,
        authenticator="idp-auto-link",
        requirement="ALTERNATIVE",
        priority=40,
        opts=resource_options,
    )
    # OL - First login flow [END]
    # OL Data Platform Realm - Authentication Flows[END]

    # OL Data Platform - Touchstone SAML [START]
    ol_data_platform_touchstone_saml_identity_provider = keycloak.saml.IdentityProvider(
        "ol-data-platform-touchstone-idp",
        realm=ol_data_platform_realm.id,
        alias="touchstone-idp",
        display_name="MIT Touchstone",
        entity_id=f"{keycloak_url}/realms/ol-data-platform",
        name_id_policy_format="Unspecified",
        force_authn=False,
        post_binding_response=True,
        post_binding_authn_request=True,
        principal_type="ATTRIBUTE",
        principal_attribute="urn:oid:1.3.6.1.4.1.5923.1.1.1.6",
        single_sign_on_service_url="https://idp.mit.edu/idp/profile/SAML2/POST/SSO",
        trust_email=True,
        validate_signature=True,
        signing_certificate=mit_touchstone_cert,
        want_assertions_encrypted=True,
        want_assertions_signed=True,
        opts=resource_options,
        first_broker_login_flow_alias=ol_data_platform_touchstone_first_login_flow.alias,
    )
    (
        keycloak.AttributeImporterIdentityProviderMapper(
            "ol-data-platform-touchstone-saml-email-attribute",
            name="ol-data-platform-touchstone-saml-email-attribute",
            realm=ol_data_platform_realm.id,
            attribute_name="mail",
            identity_provider_alias=ol_data_platform_touchstone_saml_identity_provider.alias,
            user_attribute="email",
            extra_config={
                "syncMode": "INHERIT",
            },
            opts=resource_options,
        ),
    )
    (
        keycloak.AttributeImporterIdentityProviderMapper(
            "ol-data-platform-touchstone-saml-last-name-attribute",
            name="ol-data-platform-touchstone-saml-last-name-attribute",
            realm=ol_data_platform_realm.id,
            attribute_name="sn",
            identity_provider_alias=ol_data_platform_touchstone_saml_identity_provider.alias,
            user_attribute="lastName",
            extra_config={
                "syncMode": "INHERIT",
            },
            opts=resource_options,
        ),
    )
    (
        keycloak.AttributeImporterIdentityProviderMapper(
            "ol-data-platform-touchstone-saml-first-name-attribute",
            name="ol-data-platform-touchstone-saml-first-name-attribute",
            realm=ol_data_platform_realm.id,
            attribute_name="givenName",
            identity_provider_alias=ol_data_platform_touchstone_saml_identity_provider.alias,
            user_attribute="firstName",
            extra_config={
                "syncMode": "INHERIT",
            },
            opts=resource_options,
        ),
    )
    keycloak.AttributeImporterIdentityProviderMapper(
        "ol-data-platform-touchstone-saml-full-name-attribute",
        name="ol-data-platform-touchstone-saml-full-name-attribute",
        realm=ol_data_platform_realm.id,
        attribute_name="displayName",
        identity_provider_alias=ol_data_platform_touchstone_saml_identity_provider.alias,
        user_attribute="fullName",
        extra_config={
            "syncMode": "INHERIT",
        },
        opts=resource_options,
    )
    # OL Data Platform - Touchstone SAML [END]
    return ol_data_platform_realm
