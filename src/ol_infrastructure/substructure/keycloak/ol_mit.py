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
from pulumi import Config, InvokeOptions, Output, ResourceOptions

from ol_infrastructure.substructure.keycloak.org_sso_helpers import (
    NameIdFormat,
    SamlIdpConfig,
    onboard_saml_idp,
)


def create_ol_mit_realm(  # noqa: PLR0913
    keycloak_provider: keycloak.Provider,
    keycloak_url: str,
    env_name: str,
    stack_info,
    mit_email_password: str,
    mit_email_username: str,
    mit_email_host: str,
    mit_ldap_bind_password: str,
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
    # With LDAP federation enabled and import_enabled=True:
    # - New users: idp-create-user-if-unique searches Keycloak AND LDAP.  When
    #   the user is found in LDAP they are imported with federationLink=mit-ldap
    #   before idp-auto-link runs, so the resulting account carries both the SAML
    #   identity link and the LDAP federation link.  Group membership is resolved
    #   at every subsequent login via the LDAP group mapper.
    # - Existing users (logged in before LDAP federation was added): their
    #   Keycloak record is already local (no federationLink).  The first-login
    #   flow will not run again for them, so their account is never linked to
    #   LDAP and groups will not be resolved automatically.
    #
    # REMEDIATION for existing users: run a one-time full LDAP sync after
    # deploying the federation.  Keycloak will match each LDAP user to the
    # existing local record by username and update the federationLink so that
    # group resolution starts working on the next login:
    #   POST /admin/realms/ol-mit/user-storage/<id>/sync?action=triggerFullSync
    # or via Admin UI: User Federation → mit-ldap → Action → Sync all users.

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
    # Uses the Okta metadata endpoint so that the cert and SSO URL are resolved
    # at deploy time and Keycloak can auto-refresh them at runtime.
    onboard_saml_idp(
        SamlIdpConfig(
            idp_alias="touchstone-idp",
            idp_display_name="MIT Touchstone",
            org_saml_metadata_url=(
                keycloak_realm_config.get("ol-mit-touchstone-metadata-url")
                or (
                    "https://okta.mit.edu/app/exk12ad6wcgegsrLi698/sso/saml/metadata"
                    if stack_info.env_suffix == "production"
                    else "https://okta.mit.edu/app/exk128ohli7aTT5xA698/sso/saml/metadata"
                )
            ),
            keycloak_url=keycloak_url,
            realm_id=ol_mit_realm.id,
            realm_name="ol-mit",
            first_login_flow=ol_mit_touchstone_first_login_flow,
            resource_options=resource_options,
            # Okta sends the email as the NameID (unspecified format); use SUBJECT
            # as the principal since eduPersonPrincipalName arrives empty.
            principal_type="SUBJECT",
            attribute_name_map={
                "email": "email",
                "firstName": "firstName",
                "lastName": "lastName",
            },
            want_assertions_encrypted=False,
            want_assertions_signed=True,
            name_id_format=NameIdFormat.unspecified,
        )
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
        service_accounts_enabled=True,
        valid_redirect_uris=keycloak_realm_config.get_object(
            "ol-mit-ovs-redirect-uris"
        ),
        valid_post_logout_redirect_uris=keycloak_realm_config.get_object(
            "ol-mit-ovs-logout-uris"
        ),
        web_origins=keycloak_realm_config.get_object("ol-mit-ovs-web-origins"),
        opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
    )

    # Grant the service account realm-management roles for user and group management.
    # These are consumed by the moira-to-keycloak migration management commands via
    # the Keycloak admin API (client_credentials grant against /realms/master).
    realm_management_client = keycloak.openid.get_client(
        realm_id=ol_mit_realm.id,
        client_id="realm-management",
        opts=InvokeOptions(provider=keycloak_provider),
    )
    for resource_name, role in [
        ("ol-mit-ovs-sa-manage-users", "manage-users"),
        ("ol-mit-ovs-sa-view-users", "view-users"),
        ("ol-mit-ovs-sa-query-users", "query-users"),
        ("ol-mit-ovs-sa-query-groups", "query-groups"),
    ]:
        keycloak.openid.ClientServiceAccountRole(
            resource_name,
            realm_id=ol_mit_realm.id,
            service_account_user_id=ol_mit_ovs_client.service_account_user_id,
            client_id=realm_management_client.id,
            role=role,
            opts=resource_options,
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

    # social_core.backends.keycloak.KeycloakOAuth2.user_data() decodes the access
    # token and validates aud == client_id.  Keycloak does not include the client
    # in the aud claim by default; this mapper re-enables it.
    # See https://issues.redhat.com/browse/KEYCLOAK-6638
    keycloak.openid.AudienceProtocolMapper(
        "ol-mit-ovs-audience-mapper",
        realm_id=ol_mit_realm.id,
        client_id=ol_mit_ovs_client.id,
        name="audience",
        included_client_audience=ol_mit_ovs_client.client_id,
        add_to_id_token=True,
        add_to_access_token=True,
        opts=resource_options,
    )

    # MIT LDAP FEDERATION [START]
    # Read-only federation against MIT's Okta LDAP interface to enrich users
    # already created via Touchstone SAML and to assign group memberships.
    #
    # The Okta LDAP interface exposes users under ou=users and groups under
    # ou=groups.  User entries do NOT carry a virtual memberOf attribute, so the
    # group mapper uses LOAD_GROUPS_BY_MEMBER_ATTRIBUTE to look up memberships
    # by querying group entries directly.
    #
    # Username linkage: Touchstone sends kerberos@mit.edu as the SAML NameID
    # (SUBJECT principal); the LDAP uid attribute is also kerberos@mit.edu, so
    # users created on first Touchstone login are automatically linked to their
    # LDAP counterpart on the next sync / login.
    #
    # PREREQUISITE: The open-learning-ldap.service account must have "Read
    # Groups" scope enabled in Okta Admin → Security → API → LDAP Interface
    # before the group mapper can populate memberships.  The user-attribute
    # sync works without that permission.
    ol_mit_ldap = keycloak.ldap.UserFederation(
        "ol-mit-ldap",
        name="mit-ldap",
        realm_id=ol_mit_realm.id,
        connection_url="ldaps://mitprod.ldap.okta.com",
        bind_dn="uid=open-learning-ldap.service,dc=mitprod,dc=okta,dc=com",
        bind_credential=mit_ldap_bind_password,
        users_dn="ou=users,dc=mitprod,dc=okta,dc=com",
        username_ldap_attribute="uid",
        rdn_ldap_attribute="uid",
        # uniqueIdentifier is the Okta-assigned immutable GUID for each user and
        # is the correct stable identifier across renames / email changes.
        uuid_ldap_attribute="uniqueIdentifier",
        user_object_classes=["inetOrgPerson"],
        # UNSYNCED rather than READ_ONLY: users are imported from LDAP into
        # Keycloak's local DB, and writes go to the local copy only (never back
        # to LDAP).  READ_ONLY causes a ReadOnlyException when the Touchstone
        # SAML broker's afterFirstBrokerLogin flow tries to write basic
        # attributes (firstName, lastName) onto the newly-linked user.  With
        # UNSYNCED the local copy is writable; the hourly delta sync keeps it
        # current from LDAP, and the group mapper still queries LDAP directly
        # because the federationLink is preserved.
        edit_mode="UNSYNCED",
        vendor="OTHER",
        # SUBTREE is required because Okta group entries are nested three
        # levels under ou=groups (e.g.
        # cn=ol-eng-developer,cn=<app-id>,ou=apps,ou=groups,...).
        # ONE_LEVEL would only reach the immediate children of ou=groups
        # (e.g. ou=apps itself) and never find actual group objects.
        # Users are all direct children of ou=users, so SUBTREE has no
        # downside for user searches.
        search_scope="SUBTREE",
        # Restrict to active accounts only; deprovisioned users have
        # organizationalStatus set to a value other than ACTIVE.
        custom_user_search_filter="(organizationalStatus=ACTIVE)",
        # MIT emails are validated by Touchstone; no re-verification needed.
        trust_email=True,
        sync_registrations=False,
        import_enabled=True,
        connection_pooling=True,
        pagination=True,
        use_truststore_spi="ONLY_FOR_LDAPS",
        # Fail fast rather than hanging indefinitely if the Okta LDAP endpoint
        # is unreachable or slow.  Values are in milliseconds.
        connection_timeout="30s",
        read_timeout="120s",
        # Hourly delta sync picks up group-membership changes for users who have
        # already logged in.  Full sync is disabled to avoid bulk-importing every
        # MIT affiliate into Keycloak; users are provisioned on-demand via the
        # Touchstone SAML first-login flow.
        changed_sync_period=14400,
        opts=resource_options,
    )

    # MIT-specific attribute mappers.  Standard attributes (username, email,
    # firstName, lastName) are handled by the default mappers that Keycloak
    # creates automatically when delete_default_mappers is not set.
    for resource_name, mapper_name, ldap_attr, model_attr in [
        (
            "ol-mit-ldap-edu-person-principal-name-mapper",
            "eduPersonPrincipalName",
            "eduPersonPrincipalName",
            "eduPersonPrincipalName",
        ),
        (
            "ol-mit-ldap-display-name-mapper",
            "displayName",
            "displayName",
            "displayName",
        ),
        (
            "ol-mit-ldap-edu-person-primary-affiliation-mapper",
            "eduPersonPrimaryAffiliation",
            "eduPersonPrimaryAffiliation",
            "eduPersonPrimaryAffiliation",
        ),
    ]:
        keycloak.ldap.UserAttributeMapper(
            resource_name,
            realm_id=ol_mit_realm.id,
            ldap_user_federation_id=ol_mit_ldap.id,
            name=mapper_name,
            ldap_attribute=ldap_attr,
            user_model_attribute=model_attr,
            read_only=True,
            always_read_value_from_ldap=True,
            is_mandatory_in_ldap=False,
            opts=resource_options,
        )

    # Parent group for all LDAP-sourced groups.  The groups_path parameter on
    # the group mapper requires this group to exist before the first sync runs.
    ol_mit_moira_group = keycloak.Group(
        "ol-mit-moira-group",
        realm_id=ol_mit_realm.id,
        name="moira",
        opts=resource_options,
    )

    # Group mapper — resolves memberships by querying group entries directly.
    # The Okta LDAP interface does NOT expose a virtual memberOf attribute on
    # user entries, so GET_GROUPS_FROM_USER_MEMBEROF_ATTRIBUTE cannot be used.
    # Instead, LOAD_GROUPS_BY_MEMBER_ATTRIBUTE issues a search for
    # groupOfUniqueNames objects whose uniqueMember value equals the user's DN
    # (e.g. uid=tmacey@mit.edu,ou=users,dc=mitprod,dc=okta,dc=com), which is
    # how membership is actually stored in the Okta LDAP interface.
    #
    # All synced groups land under /moira to distinguish them from groups
    # managed directly in Keycloak.
    keycloak.ldap.GroupMapper(
        "ol-mit-ldap-group-mapper",
        realm_id=ol_mit_realm.id,
        ldap_user_federation_id=ol_mit_ldap.id,
        name="group-mapper",
        ldap_groups_dn="ou=groups,dc=mitprod,dc=okta,dc=com",
        group_name_ldap_attribute="cn",
        group_object_classes=["groupOfUniqueNames"],
        membership_ldap_attribute="uniqueMember",
        membership_user_ldap_attribute="uid",
        membership_attribute_type="DN",
        user_roles_retrieve_strategy="LOAD_GROUPS_BY_MEMBER_ATTRIBUTE",
        groups_path="/moira",
        mode="READ_ONLY",
        preserve_group_inheritance=False,
        drop_non_existing_groups_during_sync=True,
        ignore_missing_groups=True,
        opts=ResourceOptions.merge(
            resource_options,
            ResourceOptions(depends_on=[ol_mit_moira_group]),
        ),
    )
    # MIT LDAP FEDERATION [END]

    vault.generic.Secret(
        "ol-mit-ovs-client-vault-oidc-credentials",
        path="secret-operations/sso/ovs",
        data_json=Output.all(
            url=ol_mit_ovs_client.realm_id.apply(
                lambda realm_id: f"{keycloak_url}/realms/{realm_id}"
            ),
            server_url=keycloak_url,
            client_id=ol_mit_ovs_client.client_id,
            client_secret=ol_mit_ovs_client.client_secret,
            secret=session_secret,
            realm_id=ol_mit_ovs_client.realm_id,
            realm_name="ol-mit",
            realm_public_key=ol_mit_ovs_client.realm_id.apply(
                # social_core.backends.keycloak.KeycloakOAuth2 expects raw base64
                # key material without PEM headers — it wraps the key itself in
                # public_key(). fetch_realm_public_key returns a full PEM string,
                # so strip the headers here.
                lambda realm_id: (
                    fetch_realm_public_key_partial(realm_id)
                    .removeprefix("-----BEGIN PUBLIC KEY-----\n")
                    .removesuffix("\n-----END PUBLIC KEY-----")
                )
            ),
        ).apply(json.dumps),
    )
    # ODL VIDEO SERVICE [END]

    # OCW STUDIO [START]
    ol_mit_ocw_studio_client = keycloak.openid.Client(
        "ol-mit-ocw-studio-client",
        name="ol-mit-ocw-studio-client",
        realm_id=ol_mit_realm.id,
        client_id="ocw-studio-app",
        client_secret=keycloak_realm_config.get("ol-mit-ocw-studio-client-secret"),
        enabled=True,
        access_type="CONFIDENTIAL",
        standard_flow_enabled=True,
        implicit_flow_enabled=False,
        direct_access_grants_enabled=False,
        service_accounts_enabled=False,
        valid_redirect_uris=keycloak_realm_config.get_object(
            "ol-mit-ocw-studio-redirect-uris"
        ),
        valid_post_logout_redirect_uris=keycloak_realm_config.get_object(
            "ol-mit-ocw-studio-logout-uris"
        ),
        web_origins=keycloak_realm_config.get_object("ol-mit-ocw-studio-web-origins"),
        opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
    )

    keycloak.openid.AudienceProtocolMapper(
        "ol-mit-ocw-studio-audience-mapper",
        realm_id=ol_mit_realm.id,
        client_id=ol_mit_ocw_studio_client.id,
        name="audience",
        included_client_audience=ol_mit_ocw_studio_client.client_id,
        add_to_id_token=True,
        add_to_access_token=True,
        opts=resource_options,
    )

    vault.generic.Secret(
        "ol-mit-ocw-studio-client-vault-oidc-credentials",
        path="secret-operations/sso/ocw-studio",
        data_json=Output.all(
            url=ol_mit_ocw_studio_client.realm_id.apply(
                lambda realm_id: f"{keycloak_url}/realms/{realm_id}"
            ),
            server_url=keycloak_url,
            client_id=ol_mit_ocw_studio_client.client_id,
            client_secret=ol_mit_ocw_studio_client.client_secret,
            secret=session_secret,
            realm_id=ol_mit_ocw_studio_client.realm_id,
            realm_name="ol-mit",
            realm_public_key=ol_mit_ocw_studio_client.realm_id.apply(
                lambda realm_id: (
                    fetch_realm_public_key_partial(realm_id)
                    .removeprefix("-----BEGIN PUBLIC KEY-----\n")
                    .removesuffix("\n-----END PUBLIC KEY-----")
                )
            ),
        ).apply(json.dumps),
    )
    # OCW STUDIO [END]

    return ol_mit_realm
