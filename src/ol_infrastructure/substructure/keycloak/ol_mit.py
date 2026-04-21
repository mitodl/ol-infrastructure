"""Keycloak realm definition for MIT-internal OL applications.

This realm serves the broad MIT audience — any authenticated MIT affiliate —
without restricting access to a particular team or set of applications. It is
intentionally kept free of application-specific clients so that those can be
layered on later.  Group and role management is first-class here to prepare for
eventual MIT LDAP federation.

Authentication is exclusively through MIT Touchstone (SAML). Local accounts are
reserved for break-glass administrative access; there is no public registration.
"""

import json

import pulumi_keycloak as keycloak
import pulumi_vault as vault
from pulumi import Config, Output, ResourceOptions


def create_ol_mit_realm(  # noqa: PLR0913
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
    """Create the OL MIT realm for MIT-internal applications.

    This realm is designed for applications that should be accessible to the
    broad MIT community (any MIT affiliate), with group/role-based access
    control and an eventual path to MIT LDAP federation for group population.
    """
    resource_options = ResourceOptions(provider=keycloak_provider)
    keycloak_realm_config = Config("keycloak_realm")

    if stack_info.env_suffix == "production":
        derived_relying_party_id = "sso.ol.mit.edu"
    else:
        derived_relying_party_id = f"sso-{stack_info.env_suffix}.ol.mit.edu"

    ol_mit_realm = keycloak.Realm(
        "ol-mit",
        realm="ol-mit",
        display_name="OL MIT",
        display_name_html="<b>OL MIT</b>",
        enabled=True,
        account_theme="keycloak.v3",
        admin_theme="keycloak.v2",
        login_theme="keycloak.v2",
        email_theme="keycloak",
        access_code_lifespan="30m",
        access_code_lifespan_user_action="15m",
        attributes={
            "business_unit": f"operations-{env_name}",
        },
        # MIT Touchstone is the canonical identity source; local accounts are
        # reserved for break-glass admin access only.
        registration_allowed=False,
        registration_email_as_username=True,
        login_with_email_allowed=True,
        duplicate_emails_allowed=False,
        reset_password_allowed=False,
        # Touchstone validates the MIT identity; no Keycloak-side email check needed.
        verify_email=False,
        # Enforce a strong password policy for the local break-glass accounts.
        password_policy=(  # noqa: S106 # pragma: allowlist secret
            "length(12) and upperCase(1) and lowerCase(1) and digits(1) and "
            "specialChars(1) and notUsername and notEmail and passwordHistory(5) "
            "and forceExpiredPasswordChange(120)"
        ),
        web_authn_passwordless_policy={
            "relying_party_entity_name": f"mit-ol-sso-{stack_info.env_suffix}",
            "relying_party_id": derived_relying_party_id,
            "require_resident_key": "Yes",
            "user_verification_requirement": "required",
        },
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
            from_display_name="Identity - OL MIT",
            host=mit_email_host,
            port="587",
            reply_to="odl-devops@mit.edu",
            reply_to_display_name="Identity - OL MIT",
            starttls=True,
        ),
        ssl_required="external",
        offline_session_idle_timeout="168h",
        sso_session_idle_timeout="2h",
        sso_session_max_lifespan="24h",
        opts=resource_options,
    )

    keycloak.RealmEvents(
        "ol-mit-realm-events",
        realm_id=ol_mit_realm.realm,
        events_enabled=True,
        events_expiration=604800,
        admin_events_enabled=True,
        admin_events_details_enabled=True,
        enabled_event_types=[],
        events_listeners=["jboss-logging"],
        opts=resource_options,
    )

    # Touchstone provides verified email addresses; no Keycloak email step needed.
    keycloak.RequiredAction(
        "ol-mit-verify-email",
        realm_id=ol_mit_realm.realm,
        alias="VERIFY_EMAIL",
        default_action=False,
        enabled=False,
        opts=resource_options,
    )

    # Realm roles for broad access control.  Applications may introduce their own
    # client roles; these realm roles serve as a cross-cutting layer and as targets
    # for future LDAP group-to-role mapping.
    keycloak.Role(
        "ol-mit-admin-role",
        realm_id=ol_mit_realm.id,
        name="admin",
        description="Full administrative access to the OL MIT realm",
        opts=resource_options,
    )

    keycloak.Role(
        "ol-mit-user-role",
        realm_id=ol_mit_realm.id,
        name="user",
        description=(
            "Standard MIT user role; pre-created for future IdP mapper or LDAP "
            "group assignment — not automatically granted on first login"
        ),
        opts=resource_options,
    )

    # Authentication flows
    # Browser flow
    # 1. Cookie         - ALTERNATIVE (re-use existing session)
    # 2. IdP redirector - ALTERNATIVE (auto-sends MIT affiliates to Touchstone)
    # 3. Local subflow  - ALTERNATIVE (username + password for break-glass admins)
    #
    # The IdP redirector is configured with defaultProvider=touchstone-idp so that
    # unauthenticated users are forwarded to Touchstone automatically.  Break-glass
    # local accounts can bypass the redirect via the Keycloak admin console or by
    # appending ?kc_idp_hint= (empty) to the login URL.

    ol_browser_mit_flow = keycloak.authentication.Flow(
        "ol-browser-mit-flow",
        realm_id=ol_mit_realm.id,
        alias="ol-browser-mit-flow",
        opts=resource_options,
    )

    keycloak.authentication.Execution(
        "ol-browser-mit-auth-cookie",
        realm_id=ol_mit_realm.id,
        parent_flow_alias=ol_browser_mit_flow.alias,
        authenticator="auth-cookie",
        requirement="ALTERNATIVE",
        priority=10,
        opts=resource_options,
    )

    ol_browser_mit_idp_redirector = keycloak.authentication.Execution(
        "ol-browser-mit-idp-redirector",
        realm_id=ol_mit_realm.id,
        parent_flow_alias=ol_browser_mit_flow.alias,
        authenticator="identity-provider-redirector",
        requirement="ALTERNATIVE",
        priority=20,
        opts=resource_options,
    )

    # Automatically send users to MIT Touchstone; break-glass admins can bypass by
    # passing an empty kc_idp_hint query param or using the admin console.
    keycloak.authentication.ExecutionConfig(
        "ol-mit-idp-redirector-config",
        realm_id=ol_mit_realm.id,
        execution_id=ol_browser_mit_idp_redirector.id,
        alias="ol-mit-idp-redirector-config",
        config={"defaultProvider": "touchstone-idp"},
        opts=resource_options,
    )

    # Local username + password subflow for break-glass admin access.
    ol_browser_mit_local_subflow = keycloak.authentication.Subflow(
        "ol-browser-mit-local-subflow",
        realm_id=ol_mit_realm.id,
        alias="ol-browser-mit-local-subflow",
        parent_flow_alias=ol_browser_mit_flow.alias,
        provider_id="basic-flow",
        requirement="ALTERNATIVE",
        priority=30,
        opts=resource_options,
    )

    keycloak.authentication.Execution(
        "ol-browser-mit-username-password-form",
        realm_id=ol_mit_realm.id,
        parent_flow_alias=ol_browser_mit_local_subflow.alias,
        authenticator="auth-username-password-form",
        requirement="REQUIRED",
        priority=10,
        opts=resource_options,
    )

    keycloak.authentication.Bindings(
        "ol-mit-browser-authentication-binding",
        realm_id=ol_mit_realm.id,
        browser_flow=ol_browser_mit_flow.alias,
        opts=resource_options,
    )

    # First-broker-login flow
    # Used when a Touchstone user logs in for the first time.  Profile review is
    # REQUIRED so that missing attributes are collected, then the user is either
    # created fresh or linked to an existing account with the same Touchstone
    # principal.
    #
    # NOTE: The auto-link step (idp-auto-link) silently links an incoming Touchstone
    # identity to an existing local account that shares the same username/email.
    # This is intentional for a Touchstone-only realm where the MIT Kerberos
    # principal (eduPersonPrincipalName) is the canonical identifier.  Revisit this
    # if MIT LDAP federation is later enabled for the same realm to avoid silent
    # duplicate-linking.

    ol_mit_touchstone_first_login_flow = keycloak.authentication.Flow(
        "ol-mit-touchstone-first-login-flow",
        realm_id=ol_mit_realm.id,
        alias="ol-mit-first-login-flow",
        opts=resource_options,
    )

    ol_mit_first_login_review_profile = keycloak.authentication.Execution(
        "ol-mit-touchstone-first-login-flow-review-profile",
        realm_id=ol_mit_realm.id,
        parent_flow_alias=ol_mit_touchstone_first_login_flow.alias,
        authenticator="idp-review-profile",
        priority=10,
        requirement="REQUIRED",
        opts=resource_options,
    )

    keycloak.authentication.ExecutionConfig(
        "ol-mit-touchstone-first-login-review-profile-config",
        realm_id=ol_mit_realm.id,
        execution_id=ol_mit_first_login_review_profile.id,
        alias="ol-mit-review-profile-config",
        config={"updateProfileOnFirstLogin": "missing"},
        opts=resource_options,
    )

    ol_mit_touchstone_user_creation_subflow = keycloak.authentication.Subflow(
        "ol-mit-touchstone-user-creation-or-linking-subflow",
        realm_id=ol_mit_realm.id,
        alias="ol-mit-touchstone-first-broker-login-user-creation-or-linking",
        parent_flow_alias=ol_mit_touchstone_first_login_flow.alias,
        provider_id="basic-flow",
        priority=20,
        requirement="REQUIRED",
        opts=resource_options,
    )

    keycloak.authentication.Execution(
        "ol-mit-touchstone-create-user-if-unique",
        realm_id=ol_mit_realm.id,
        parent_flow_alias=ol_mit_touchstone_user_creation_subflow.alias,
        authenticator="idp-create-user-if-unique",
        priority=30,
        requirement="ALTERNATIVE",
        opts=resource_options,
    )

    keycloak.authentication.Execution(
        "ol-mit-touchstone-automatically-set-existing-user",
        realm_id=ol_mit_realm.id,
        parent_flow_alias=ol_mit_touchstone_user_creation_subflow.alias,
        authenticator="idp-auto-link",
        requirement="ALTERNATIVE",
        priority=40,
        opts=resource_options,
    )

    # MIT Touchstone SAML identity provider
    # The Touchstone SP entity ID is derived from this realm's well-known URL.
    # principal_attribute carries the MIT eduPersonPrincipalName OID (Kerberos
    # principal, e.g. jsmith@MIT.EDU) which provides a stable, unique username
    # that will align with LDAP when federation is introduced later.

    touchstone_sso_url = (
        keycloak_realm_config.get("ol-mit-touchstone-single-sign-on-service-url")
        or "https://idp.mit.edu/idp/profile/SAML2/POST/SSO"
    )
    touchstone_cert = (
        keycloak_realm_config.get("ol-mit-touchstone-sig-cert") or mit_touchstone_cert
    )

    ol_mit_touchstone_saml_idp = keycloak.saml.IdentityProvider(
        "ol-mit-touchstone-idp",
        realm=ol_mit_realm.id,
        alias="touchstone-idp",
        display_name="MIT Touchstone",
        entity_id=f"{keycloak_url}/realms/ol-mit",
        name_id_policy_format="Unspecified",
        force_authn=False,
        post_binding_response=True,
        post_binding_authn_request=True,
        # Use the MIT eduPersonPrincipalName as the stable identifier so that the
        # resulting Keycloak username will match the eventual LDAP uid attribute.
        principal_type="ATTRIBUTE",
        principal_attribute="urn:oid:1.3.6.1.4.1.5923.1.1.1.6",
        single_sign_on_service_url=touchstone_sso_url,
        trust_email=True,
        validate_signature=True,
        signing_certificate=touchstone_cert,
        want_assertions_encrypted=True,
        want_assertions_signed=True,
        first_broker_login_flow_alias=ol_mit_touchstone_first_login_flow.alias,
        opts=resource_options,
    )

    # Attribute mappers — import standard MIT Touchstone SAML attributes into
    # the corresponding Keycloak user profile fields.
    keycloak.AttributeImporterIdentityProviderMapper(
        "ol-mit-touchstone-saml-email-attribute",
        name="ol-mit-touchstone-saml-email-attribute",
        realm=ol_mit_realm.id,
        attribute_name="mail",
        identity_provider_alias=ol_mit_touchstone_saml_idp.alias,
        user_attribute="email",
        extra_config={"syncMode": "INHERIT"},
        opts=resource_options,
    )

    keycloak.AttributeImporterIdentityProviderMapper(
        "ol-mit-touchstone-saml-last-name-attribute",
        name="ol-mit-touchstone-saml-last-name-attribute",
        realm=ol_mit_realm.id,
        attribute_name="sn",
        identity_provider_alias=ol_mit_touchstone_saml_idp.alias,
        user_attribute="lastName",
        extra_config={"syncMode": "INHERIT"},
        opts=resource_options,
    )

    keycloak.AttributeImporterIdentityProviderMapper(
        "ol-mit-touchstone-saml-first-name-attribute",
        name="ol-mit-touchstone-saml-first-name-attribute",
        realm=ol_mit_realm.id,
        attribute_name="givenName",
        identity_provider_alias=ol_mit_touchstone_saml_idp.alias,
        user_attribute="firstName",
        extra_config={"syncMode": "INHERIT"},
        opts=resource_options,
    )

    keycloak.AttributeImporterIdentityProviderMapper(
        "ol-mit-touchstone-saml-full-name-attribute",
        name="ol-mit-touchstone-saml-full-name-attribute",
        realm=ol_mit_realm.id,
        attribute_name="displayName",
        identity_provider_alias=ol_mit_touchstone_saml_idp.alias,
        user_attribute="fullName",
        extra_config={"syncMode": "INHERIT"},
        opts=resource_options,
    )

    # ODL VIDEO SERVICE [START]
    ol_mit_ovs_client = keycloak.openid.Client(
        "ol-mit-ovs-client",
        name="ol-mit-ovs-client",
        realm_id=ol_mit_realm.id,
        client_id="odl-video-app",
        client_secret=keycloak_realm_config.get("ol-mit-ovs-client-secret"),
        enabled=True,
        access_type="CONFIDENTIAL",
        standard_flow_enabled=True,
        implicit_flow_enabled=False,
        # Enabled to allow password-based auth for admin/testing workflows.
        direct_access_grants_enabled=True,
        service_accounts_enabled=False,
        valid_redirect_uris=keycloak_realm_config.get_object(
            "ol-mit-ovs-redirect-uris"
        ),
        valid_post_logout_redirect_uris=keycloak_realm_config.get_object(
            "ol-mit-ovs-logout-uris"
        ),
        web_origins=keycloak_realm_config.get_object("ol-mit-ovs-web-origins"),
        opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
    )

    # The OVS pipeline (assign_user_groups) reads the `user_groups` claim to map
    # Keycloak groups to Django is_staff / is_superuser flags.
    keycloak.openid.GroupMembershipProtocolMapper(
        "ol-mit-ovs-user-groups-mapper",
        realm_id=ol_mit_realm.id,
        client_id=ol_mit_ovs_client.id,
        name="user_groups",
        claim_name="user_groups",
        full_path=True,
        add_to_id_token=True,
        add_to_access_token=True,
        add_to_userinfo=True,
        opts=resource_options,
    )

    vault.generic.Secret(
        "ol-mit-ovs-client-vault-oidc-credentials",
        path="secret-operations/sso/ovs",
        data_json=Output.all(
            url=ol_mit_ovs_client.realm_id.apply(
                lambda realm_id: f"{keycloak_url}/realms/{realm_id}"
            ),
            client_id=ol_mit_ovs_client.client_id,
            client_secret=ol_mit_ovs_client.client_secret,
            secret=session_secret,
            realm_id=ol_mit_ovs_client.realm_id,
            realm_name="ol-mit",
            realm_public_key=ol_mit_ovs_client.realm_id.apply(
                fetch_realm_public_key_partial
            ),
        ).apply(json.dumps),
    )
    # ODL VIDEO SERVICE [END]

    return ol_mit_realm
