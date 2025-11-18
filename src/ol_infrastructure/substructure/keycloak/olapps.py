"""Keycloak realm definition for OL applications."""

import json
from pathlib import Path

import pulumi_keycloak as keycloak
import pulumi_vault as vault
from pulumi import Config, InvokeOptions, Output, ResourceOptions

from bridge.lib.magic_numbers import SECONDS_IN_ONE_DAY
from ol_infrastructure.substructure.keycloak.org_flows import (
    create_organization_browser_flows,
    create_organization_first_broker_login_flows,
    create_organization_scope,
)
from ol_infrastructure.substructure.keycloak.org_sso_helpers import (
    NameIdFormat,
    OIDCIdpConfig,
    OrgConfig,
    SamlIdpConfig,
    create_org_for_learn,
    onboard_oidc_org,
    onboard_saml_org,
)


def create_olapps_realm(  # noqa: PLR0913, PLR0915
    keycloak_provider: keycloak.Provider,
    keycloak_url: str,
    env_name: str,
    stack_info,
    mailgun_email_password: str,
    mailgun_email_username: str,
    mailgun_email_host: str,
    mit_touchstone_cert: str,
    session_secret: str,
    fetch_realm_public_key_partial,
):
    """Create the OL Apps realm and all of its resources."""
    resource_options = ResourceOptions(provider=keycloak_provider)
    keycloak_realm_config = Config("keycloak_realm")
    captcha_domain = "www.recaptcha.net"
    ol_apps_realm = keycloak.Realm(
        "olapps",
        access_code_lifespan="30m",
        access_code_lifespan_user_action="15m",
        attributes={
            "business_unit": f"operations-{env_name}",
        },
        display_name="MIT Learn",
        display_name_html="<b>MIT Learn</b>",
        enabled=True,
        email_theme="ol",
        login_theme="ol-learn",
        duplicate_emails_allowed=False,
        otp_policy=keycloak.RealmOtpPolicyArgs(
            algorithm="HmacSHA256",
            digits=6,
            initial_counter=2,
            look_ahead_window=1,
            period=30,
            type="totp",
        ),
        realm="olapps",
        registration_allowed=True,
        reset_password_allowed=True,
        login_with_email_allowed=True,
        registration_email_as_username=True,
        verify_email=True,
        password_policy=(  # noqa: S106 # pragma: allowlist secret
            "length(8) and notUsername and notEmail"
        ),
        security_defenses=keycloak.RealmSecurityDefensesArgs(
            brute_force_detection=keycloak.RealmSecurityDefensesBruteForceDetectionArgs(
                failure_reset_time_seconds=43200,
                max_failure_wait_seconds=3600,
                max_login_failures=10,
                minimum_quick_login_wait_seconds=60,
                permanent_lockout=True,
                quick_login_check_milli_seconds=700,
                wait_increment_seconds=300,
            ),
            headers=keycloak.RealmSecurityDefensesHeadersArgs(
                content_security_policy=(
                    f"frame-src 'self' https://{captcha_domain}; frame-ancestors 'self'; object-src 'none';"  # noqa: E501
                ),
                content_security_policy_report_only="",
                strict_transport_security="max-age=31536000; includeSubDomains",
                x_content_type_options="nosniff",
                x_frame_options=f"https://{captcha_domain}",
                x_robots_tag="none",
                x_xss_protection="1; mode=block",
            ),
        ),
        smtp_server=keycloak.RealmSmtpServerArgs(
            auth=keycloak.RealmSmtpServerAuthArgs(
                password=mailgun_email_password,
                username=mailgun_email_username,
            ),
            from_=mailgun_email_username,
            from_display_name="MIT Learn",
            host=mailgun_email_host,
            port="465",
            ssl=True,
            starttls=False,
        ),
        ssl_required="external",
        offline_session_idle_timeout="168h",
        organizations_enabled=True,
        sso_session_idle_timeout="336h",
        sso_session_max_lifespan="336h",
        opts=resource_options,
    )

    keycloak.RealmEvents(
        "realmEvents",
        realm_id=ol_apps_realm.realm,
        events_enabled=True,
        events_expiration=SECONDS_IN_ONE_DAY,
        admin_events_enabled=True,
        admin_events_details_enabled=True,
        events_listeners=["jboss-logging"],
        opts=resource_options,
    )

    keycloak.RequiredAction(
        "ol-apps-configure-totp",
        realm_id=ol_apps_realm.realm,
        alias="CONFIGURE_TOTP",
        default_action=False,
        enabled=True,
        opts=resource_options,
    )

    keycloak.RequiredAction(
        "ol-apps-verify-email",
        realm_id=ol_apps_realm.realm,
        alias="VERIFY_EMAIL",
        default_action=True,
        enabled=True,
        opts=resource_options,
    )

    keycloak.RequiredAction(
        "ol-apps-update-email",
        realm_id=ol_apps_realm.realm,
        alias="UPDATE_EMAIL",
        default_action=False,
        enabled=True,
        opts=resource_options,
    )

    keycloak.RequiredAction(
        "ol-apps-update-password",
        realm_id=ol_apps_realm.realm,
        alias="UPDATE_PASSWORD",
        default_action=False,
        enabled=True,
        opts=resource_options,
    )

    keycloak.RealmUserProfile(
        "olapps-user-profile",
        realm_id=ol_apps_realm.realm,
        attributes=[
            keycloak.RealmUserProfileAttributeArgs(
                name="fullName",
                display_name="${fullName}",
                validators=[
                    keycloak.RealmUserProfileAttributeValidatorArgs(
                        name="length",
                        config={"max": "512"},
                    ),
                    keycloak.RealmUserProfileAttributeValidatorArgs(
                        name="person-name-prohibited-characters", config={}
                    ),
                ],
                required_for_roles=["user"],
                permissions=keycloak.RealmUserProfileAttributePermissionsArgs(
                    views=["admin", "user"], edits=["admin", "user"]
                ),
            ),
            keycloak.RealmUserProfileAttributeArgs(
                name="email",
                display_name="${email}",
                validators=[
                    keycloak.RealmUserProfileAttributeValidatorArgs(
                        name="email",
                        config={},
                    ),
                    keycloak.RealmUserProfileAttributeValidatorArgs(
                        name="length",
                        config={"max": "255"},
                    ),
                ],
                required_for_roles=["user"],
                permissions=keycloak.RealmUserProfileAttributePermissionsArgs(
                    views=["admin", "user"], edits=["admin", "user"]
                ),
            ),
            keycloak.RealmUserProfileAttributeArgs(
                name="username",
                display_name="${username}",
                validators=[
                    keycloak.RealmUserProfileAttributeValidatorArgs(
                        name="length",
                        config={"min": "3", "max": "255"},
                    ),
                    keycloak.RealmUserProfileAttributeValidatorArgs(
                        name="username-prohibited-characters", config={}
                    ),
                    keycloak.RealmUserProfileAttributeValidatorArgs(
                        name="up-username-not-idn-homograph", config={}
                    ),
                ],
                permissions=keycloak.RealmUserProfileAttributePermissionsArgs(
                    views=["admin", "user"], edits=["admin", "user"]
                ),
            ),
            keycloak.RealmUserProfileAttributeArgs(
                name="firstName",
                display_name="${firstName}",
                group="legal-address",
                validators=[
                    keycloak.RealmUserProfileAttributeValidatorArgs(
                        name="length",
                        config={"max": "255"},
                    ),
                    keycloak.RealmUserProfileAttributeValidatorArgs(
                        name="person-name-prohibited-characters", config={}
                    ),
                ],
                required_for_roles=[],
                permissions=keycloak.RealmUserProfileAttributePermissionsArgs(
                    views=["admin", "user"], edits=["admin", "user"]
                ),
            ),
            keycloak.RealmUserProfileAttributeArgs(
                name="lastName",
                display_name="${lastName}",
                group="legal-address",
                validators=[
                    keycloak.RealmUserProfileAttributeValidatorArgs(
                        name="length",
                        config={"max": "255"},
                    ),
                    keycloak.RealmUserProfileAttributeValidatorArgs(
                        name="person-name-prohibited-characters", config={}
                    ),
                ],
                required_for_roles=[],
                permissions=keycloak.RealmUserProfileAttributePermissionsArgs(
                    views=["admin", "user"], edits=["admin", "user"]
                ),
            ),
            keycloak.RealmUserProfileAttributeArgs(
                name="emailOptIn",
                display_name="${emailOptIn}",
                required_for_roles=[],
                permissions=keycloak.RealmUserProfileAttributePermissionsArgs(
                    views=["admin", "user"], edits=["admin", "user"]
                ),
            ),
        ],
        groups=[
            keycloak.RealmUserProfileGroupArgs(
                name="user-metadata",
                display_header="User metadata",
                display_description="Attributes, which refer to user metadata",
            ),
            keycloak.RealmUserProfileGroupArgs(
                name="legal-address",
                display_header="Legal Address",
                display_description="User's legal address",
            ),
        ],
    )

    ol_apps_profile_client_scope = keycloak.openid.ClientScope(
        "ol-profile-client-scope",
        name="ol-profile",
        realm_id=ol_apps_realm.id,
    )

    keycloak.openid.UserAttributeProtocolMapper(
        "email-optin-mapper",
        realm_id=ol_apps_realm.id,
        client_scope_id=ol_apps_profile_client_scope.id,
        name="email-optin-mapper",
        user_attribute="emailOptIn",
        claim_name="email_optin",
    )
    keycloak.openid.UserAttributeProtocolMapper(
        "fullname-mapper",
        realm_id=ol_apps_realm.id,
        client_scope_id=ol_apps_profile_client_scope.id,
        name="fullname-mapper",
        user_attribute="fullName",
        claim_name="name",
    )
    # Unified Ecommerce Client [START]
    olapps_unified_ecommerce_client = keycloak.openid.Client(
        "olapps-unified-ecommerce-client",
        name="ol-unified-ecommerce-client",
        realm_id="olapps",
        client_id="ol-unified-ecommerce-client",
        client_secret=keycloak_realm_config.get(
            "olapps-unified-ecommerce-client-secret"
        ),
        enabled=True,
        access_type="CONFIDENTIAL",
        standard_flow_enabled=True,
        implicit_flow_enabled=False,
        service_accounts_enabled=False,
        valid_redirect_uris=keycloak_realm_config.get_object(
            "olapps-unified-ecommerce-client-redirect-uris"
        ),
        valid_post_logout_redirect_uris=keycloak_realm_config.get_object(
            "olapps-unified-ecommerce-client-logout-uris"
        ),
        opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
    )
    keycloak.openid.ClientDefaultScopes(
        "olapps-unified-ecommerce-client-default-scopes",
        realm_id="olapps",
        client_id=olapps_unified_ecommerce_client.id,
        default_scopes=[
            "acr",
            "email",
            "profile",
            "role_list",
            "roles",
            "web-origins",
            "ol-profile",
            "organization",
        ],
    )
    olapps_unified_ecommerce_client_roles = (
        keycloak_realm_config.get_object("olapps-unified-ecommerce-client-roles") or []
    )
    for role in olapps_unified_ecommerce_client_roles:
        keycloak.Role(
            f"olapps-unified-ecommerce-client-{role}",
            name=role,
            realm_id="olapps",
            client_id=olapps_unified_ecommerce_client.id,
            opts=resource_options,
        )
    vault.generic.Secret(
        "olapps-unified-ecommerce-client-vault-oidc-credentials",
        path="secret-operations/sso/unified-ecommerce",
        data_json=Output.all(
            url=olapps_unified_ecommerce_client.realm_id.apply(
                lambda realm_id: f"{keycloak_url}/realms/{realm_id}"
            ),
            client_id=olapps_unified_ecommerce_client.client_id,
            client_secret=olapps_unified_ecommerce_client.client_secret,
            # This is included for the case where we are using traefik-forward-auth.
            # It requires a random secret value to be present which is independent
            # of the OAuth credentials.
            secret=session_secret,
            realm_id=olapps_unified_ecommerce_client.realm_id,
            realm_name="olapps",
            realm_public_key=olapps_unified_ecommerce_client.realm_id.apply(
                lambda realm_id: fetch_realm_public_key_partial(realm_id)
            ),
        ).apply(json.dumps),
    )
    # Unified Ecommerce Client [END]

    # Learn AI [START]
    olapps_learn_ai_client = keycloak.openid.Client(
        "olapps-learn-ai-client",
        name="ol-learn-ai-client",
        realm_id="olapps",
        client_id="ol-learn-ai-client",
        client_secret=keycloak_realm_config.get("olapps-learn-ai-client-secret"),
        enabled=True,
        access_type="CONFIDENTIAL",
        standard_flow_enabled=True,
        implicit_flow_enabled=False,
        service_accounts_enabled=False,
        valid_redirect_uris=keycloak_realm_config.get_object(
            "olapps-learn-ai-redirect-uris"
        ),
        opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
    )
    keycloak.openid.ClientDefaultScopes(
        "olapps-learn-ai-client-default-scopes",
        realm_id="olapps",
        client_id=olapps_learn_ai_client.id,
        default_scopes=[
            "acr",
            "email",
            "profile",
            "role_list",
            "roles",
            "web-origins",
            "ol-profile",
            "organization",
        ],
    )
    olapps_learn_ai_client_roles = (
        keycloak_realm_config.get_object("olapps-learn-ai-client-roles") or []
    )
    for role in olapps_learn_ai_client_roles:
        keycloak.Role(
            f"olapps-learn-ai-client-{role}",
            name=role,
            realm_id="olapps",
            client_id=olapps_learn_ai_client.id,
            opts=resource_options,
        )
    vault.generic.Secret(
        "olapps-learn-ai-client-vault-oidc-credentials",
        path="secret-operations/sso/learn-ai",
        data_json=Output.all(
            url=olapps_learn_ai_client.realm_id.apply(
                lambda realm_id: f"{keycloak_url}/realms/{realm_id}"
            ),
            client_id=olapps_learn_ai_client.client_id,
            client_secret=olapps_learn_ai_client.client_secret,
            # This is included for the case where we are using traefik-forward-auth.
            # It requires a random secret value to be present which is independent
            # of the OAuth credentials.
            secret=session_secret,
            realm_id=olapps_learn_ai_client.realm_id,
            realm_name="olapps",
            realm_public_key=olapps_learn_ai_client.realm_id.apply(
                lambda realm_id: fetch_realm_public_key_partial(realm_id)
            ),
        ).apply(json.dumps),
    )
    # Learn AI [END]

    # MIT LEARN [START]
    if keycloak_realm_config.get("olapps-mitlearn-client-secret"):
        olapps_mitlearn_client = keycloak.openid.Client(
            "olapps-mitlearn-client",
            name="ol-mitlearn-client",
            realm_id="olapps",
            client_id="ol-mitlearn-client",
            client_secret=keycloak_realm_config.get("olapps-mitlearn-client-secret"),
            enabled=True,
            access_type="CONFIDENTIAL",
            standard_flow_enabled=True,
            implicit_flow_enabled=False,
            service_accounts_enabled=False,
            valid_redirect_uris=keycloak_realm_config.get_object(
                "olapps-mitlearn-client-redirect-uris"
            ),
            web_origins=["+"],
            opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
        )
        keycloak.openid.ClientDefaultScopes(
            "olapps-mitlearn-client-default-scopes",
            realm_id="olapps",
            client_id=olapps_mitlearn_client.id,
            default_scopes=[
                "acr",
                "email",
                "profile",
                "role_list",
                "roles",
                "web-origins",
                "ol-profile",
                "organization",
            ],
        )
        olapps_mitlearn_client_roles = keycloak_realm_config.get_object(
            "olapps-mitlearn-client-roles"
        )
        if olapps_mitlearn_client_roles:
            for role in olapps_mitlearn_client_roles:
                keycloak.Role(
                    f"olapps-mitlearn-client-{role}",
                    name=role,
                    realm_id="olapps",
                    client_id=olapps_mitlearn_client.id,
                    opts=resource_options,
                )
        vault.generic.Secret(
            "olapps-mitlearn-client-vault-oidc-credentials",
            path="secret-operations/sso/mitlearn",
            data_json=Output.all(
                url=olapps_mitlearn_client.realm_id.apply(
                    lambda realm_id: f"{keycloak_url}/realms/{realm_id}"
                ),
                client_id=olapps_mitlearn_client.client_id,
                client_secret=olapps_mitlearn_client.client_secret,
                # This is included for the case where we are using traefik-forward-auth.
                # It requires a random secret value to be present which is independent
                # of the OAuth credentials.
                secret=session_secret,
                realm_id=olapps_mitlearn_client.realm_id,
                realm_name="olapps",
                realm_public_key=olapps_mitlearn_client.realm_id.apply(
                    lambda realm_id: fetch_realm_public_key_partial(realm_id)
                ),
            ).apply(json.dumps),
        )
    # MIT LEARN [END]

    # OPEN DISCUSSIONS [START]
    olapps_open_discussions_client = keycloak.openid.Client(
        "olapps-open-discussions-client",
        name="ol-open-discussions-client",
        realm_id="olapps",
        client_id="ol-open-discussions-client",
        client_secret=keycloak_realm_config.get(
            "olapps-open-discussions-client-secret"
        ),
        enabled=True,
        access_type="CONFIDENTIAL",
        standard_flow_enabled=True,
        implicit_flow_enabled=False,
        service_accounts_enabled=False,
        valid_redirect_uris=keycloak_realm_config.get_object(
            "olapps-open-discussions-client-redirect-uris"
        ),
        opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
    )
    vault.generic.Secret(
        "olapps-open-discussions-client-vault-oidc-credentials",
        path="secret-operations/sso/open-discussions",
        data_json=Output.all(
            url=olapps_open_discussions_client.realm_id.apply(
                lambda realm_id: f"{keycloak_url}/realms/{realm_id}"
            ),
            client_id=olapps_open_discussions_client.client_id,
            client_secret=olapps_open_discussions_client.client_secret,
            # This is included for the case where we are using traefik-forward-auth.
            # It requires a random secret value to be present which is independent
            # of the OAuth credentials.
            secret=session_secret,
            realm_id=olapps_open_discussions_client.realm_id,
            realm_name="olapps",
            realm_public_key=olapps_open_discussions_client.realm_id.apply(
                lambda realm_id: fetch_realm_public_key_partial(realm_id)
            ),
        ).apply(json.dumps),
    )
    # OPEN DISCUSSIONS [END]

    # MITXONLINE SCIM [START]
    olapps_mitxonline_client = keycloak.openid.Client(
        "olapps-mitxonline-client",
        name="ol-mitxonline-client",
        realm_id="olapps",
        client_id="ol-mitxonline-client",
        client_secret=keycloak_realm_config.get("olapps-mitxonline-client-secret"),
        enabled=True,
        access_type="CONFIDENTIAL",
        standard_flow_enabled=True,
        implicit_flow_enabled=False,
        service_accounts_enabled=True,
        valid_redirect_uris=keycloak_realm_config.require_object(
            "olapps-mitxonline-client-redirect-uris"
        ),
        opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
    )
    vault.generic.Secret(
        "olapps-mitxonline-client-vault-oidc-credentials",
        path="secret-mitxonline/keycloak-scim",
        data_json=Output.all(
            url=olapps_mitxonline_client.realm_id.apply(
                lambda realm_id: f"{keycloak_url}/realms/{realm_id}"
            ),
            client_id=olapps_mitxonline_client.client_id,
            client_secret=olapps_mitxonline_client.client_secret,
            # This is included for the case where we are using traefik-forward-auth.
            # It requires a random secret value to be present which is independent
            # of the OAuth credentials.
            secret=session_secret,
            realm_id=olapps_mitxonline_client.realm_id,
            realm_name="olapps",
            realm_public_key=olapps_mitxonline_client.realm_id.apply(
                lambda realm_id: fetch_realm_public_key_partial(realm_id)
            ),
        ).apply(json.dumps),
    )

    # MITXONLINE SCIM [END]

    # MITXONLINE B2B [START]
    # This client is used by MITx Online for B2B operations via the Keycloak
    # Admin API. It requires service account roles to view realms, users, and
    # organizations.
    olapps_mitxonline_b2b_client = keycloak.openid.Client(
        "olapps-mitxonline-b2b-client",
        name="mitxonline-b2b-client",
        realm_id=ol_apps_realm.id,
        client_id="mitxonline-b2b-client",
        enabled=True,
        access_type="CONFIDENTIAL",
        standard_flow_enabled=False,
        implicit_flow_enabled=False,
        direct_access_grants_enabled=False,
        service_accounts_enabled=True,
        valid_redirect_uris=[],
        opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
    )

    # Get realm-management client to assign roles
    realm_management_client = keycloak.openid.get_client(
        realm_id=ol_apps_realm.id,
        client_id="realm-management",
        opts=InvokeOptions(provider=keycloak_provider),
    )

    # Assign required service account roles for Keycloak Admin API access
    # These roles allow the client to list/view realms, users, and organizations
    # Refactored repetitive role assignments into a loop for maintainability
    for resource_name, role in [
        ("olapps-mitxonline-b2b-client-view-realm-role", "view-realm"),
        ("olapps-mitxonline-b2b-client-view-users-role", "view-users"),
        ("olapps-mitxonline-b2b-client-query-users-role", "query-users"),
        ("olapps-mitxonline-b2b-client-manage-realm-role", "manage-realm"),
    ]:
        keycloak.openid.ClientServiceAccountRole(
            resource_name,
            realm_id=ol_apps_realm.id,
            service_account_user_id=olapps_mitxonline_b2b_client.service_account_user_id,
            client_id=realm_management_client.id,
            role=role,
            opts=resource_options,
        )

    vault.generic.Secret(
        "olapps-mitxonline-b2b-client-vault-credentials",
        path="secret-mitxonline/keycloak-admin-b2b",
        data_json=Output.all(
            url=olapps_mitxonline_b2b_client.realm_id.apply(
                lambda realm_id: f"{keycloak_url}/realms/{realm_id}"
            ),
            client_id=olapps_mitxonline_b2b_client.client_id,
            client_secret=olapps_mitxonline_b2b_client.client_secret,
            realm_id=olapps_mitxonline_b2b_client.realm_id,
            realm_name="olapps",
        ).apply(json.dumps),
    )

    # MITXONLINE B2B [END]

    # OLAPPS REALM- First login flow [START]

    # Does not require email verification or confirmation to connect with existing
    # account.
    ol_first_login_flow = create_organization_first_broker_login_flows(
        ol_apps_realm.id, "olapps", opts=resource_options
    )
    # OL - First login flow [END]

    # OL - browser flow [START]
    # username-form -> ol-auth-username-password-form
    ol_browser_flow = create_organization_browser_flows(
        ol_apps_realm.id, "olapps", opts=resource_options
    )
    # Bind the flow to the olapps realm for browser login.
    keycloak.authentication.Bindings(
        "ol-apps-flow-bindings",
        realm_id=ol_apps_realm.id,
        browser_flow=ol_browser_flow.alias,
        first_broker_login_flow=ol_first_login_flow.alias,
        opts=resource_options,
    )
    # OL - browser flow [END]
    # Ensure organization scope is present
    create_organization_scope(ol_apps_realm.id, "olapps", resource_options)
    mitlearn_domain = keycloak_realm_config.require("learn_domain")
    # Touchstone SAML [START]
    mit_mail_domains = [
        "broad.mit.edu",
        "cag.csail.mit.edu",
        "csail.mit.edu",
        "education.mit.edu",
        "ll.mit.edu",
        "math.mit.edu",
        "med.mit.edu",
        "media.mit.edu",
        "mit.edu",
        "mitimco.mit.edu",
        "mtl.mit.edu",
        "professional.mit.edu",
        "sloan.mit.edu",
        "smart.mit.edu",
        "solve.mit.edu",
        "wi.mit.edu",
    ]
    ol_apps_mit_org = keycloak.organization.Organization(
        "ol-apps-mit-organization",
        opts=resource_options,
        domains=[
            keycloak.organization.OrganizationDomainArgs(name=mit_domain, verified=True)
            for mit_domain in mit_mail_domains
        ],
        description="Massachusetts Institute of Technology",
        enabled=True,
        name="MIT",
        alias="mit",
        attributes={"slug": "MIT"},
        redirect_url=f"https://{mitlearn_domain}/dashboard/organization/mit",
        realm=ol_apps_realm.id,
    )

    ol_apps_touchstone_saml_identity_provider = keycloak.saml.IdentityProvider(
        "touchstone-idp",
        realm=ol_apps_realm.id,
        alias="touchstone-idp",
        display_name="MIT Touchstone",
        entity_id=f"{keycloak_url}/realms/olapps",
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
        first_broker_login_flow_alias=ol_first_login_flow.alias,
        org_domain="ANY",
        organization_id=ol_apps_mit_org.id,
        org_redirect_mode_email_matches=True,
        hide_on_login_page=True,
    )

    keycloak.AttributeImporterIdentityProviderMapper(
        "map-touchstone-saml-email-attribute",
        realm=ol_apps_realm.id,
        attribute_friendly_name="mail",
        identity_provider_alias=ol_apps_touchstone_saml_identity_provider.alias,
        user_attribute="email",
        extra_config={
            "syncMode": "INHERIT",
        },
        opts=resource_options,
    )
    keycloak.AttributeImporterIdentityProviderMapper(
        "map-touchstone-saml-last-name-attribute",
        realm=ol_apps_realm.id,
        attribute_friendly_name="sn",
        identity_provider_alias=ol_apps_touchstone_saml_identity_provider.alias,
        user_attribute="lastName",
        extra_config={
            "syncMode": "INHERIT",
        },
        opts=resource_options,
    )
    keycloak.AttributeImporterIdentityProviderMapper(
        "map-touchstone-saml-first-name-attribute",
        realm=ol_apps_realm.id,
        attribute_friendly_name="givenName",
        identity_provider_alias=ol_apps_touchstone_saml_identity_provider.alias,
        user_attribute="firstName",
        extra_config={
            "syncMode": "INHERIT",
        },
        opts=resource_options,
    )
    keycloak.AttributeImporterIdentityProviderMapper(
        "map-touchstone-saml-full-name-attribute",
        realm=ol_apps_realm.id,
        attribute_friendly_name="displayName",
        identity_provider_alias=ol_apps_touchstone_saml_identity_provider.alias,
        user_attribute="fullName",
        extra_config={
            "syncMode": "INHERIT",
        },
        opts=resource_options,
    )
    keycloak.HardcodedAttributeIdentityProviderMapper(
        "map-touchstone-email-opt-in-attribute",
        name="email-opt-in-default",
        realm=ol_apps_realm.id,
        identity_provider_alias=ol_apps_touchstone_saml_identity_provider.alias,
        attribute_name="emailOptIn",
        attribute_value="1",
        user_session=False,
        extra_config={
            "syncMode": "INHERIT",
        },
    )
    # Touchstone SAML [END]

    # B2B Organizations [BEGIN]
    create_org_for_learn(
        OrgConfig(
            org_domains=["moira-mit.edu"],
            org_name="MIT Moira",
            org_alias="moira",
            learn_domain=mitlearn_domain,
            realm_id=ol_apps_realm.id,
            resource_options=resource_options,
        )
    )

    create_org_for_learn(
        OrgConfig(
            org_domains=["company-x.mit.edu"],
            org_name="Company X",
            org_alias="company-x",
            learn_domain=mitlearn_domain,
            realm_id=ol_apps_realm.id,
            resource_options=resource_options,
        )
    )

    if stack_info.env_suffix == "production":
        onboard_saml_org(
            SamlIdpConfig(
                org_domains=["hhchealth.org"],
                org_name="Hartford Health Care",
                org_alias="HHC",
                org_saml_metadata_url="https://adfs.hhchealth.org/federationmetadata/2007-06/federationmetadata.xml",
                keycloak_url=keycloak_url,
                learn_domain=mitlearn_domain,
                realm_id=ol_apps_realm.id,
                first_login_flow=ol_first_login_flow,
                resource_options=resource_options,
                principal_type="ATTRIBUTE",
                principal_attribute="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
            )
        )
        onboard_saml_org(
            SamlIdpConfig(
                org_domains=[
                    "ntua.gr",
                    "mail.ntua.gr",
                    "chemeng.ntua.gr",
                    "cs.ntua.gr",
                ],
                org_name="National Technical University of Athens",
                org_alias="NTUA",
                org_saml_metadata_url="https://login.ntua.gr/metadata-signed.xml",
                principal_type="FRIENDLY_ATTRIBUTE",
                principal_attribute="mail",
                name_id_format=NameIdFormat.unspecified,
                keycloak_url=keycloak_url,
                learn_domain=mitlearn_domain,
                realm_id=ol_apps_realm.id,
                first_login_flow=ol_first_login_flow,
                resource_options=resource_options,
                attribute_map={
                    "email": "mail",
                    "firstName": "givenName",
                    "lastName": "sn",
                    "fullName": "cn",
                },
                want_assertions_encrypted=True,
                want_assertions_signed=True,
            )
        )
        onboard_saml_org(
            SamlIdpConfig(
                org_domains=["ceide.unam.mx"],
                org_name="Coordinación de Evaluación, Innovación y Desarrollo Educativos, UNAM",  # noqa: E501
                org_alias="CEIDE",
                org_saml_metadata_xml=Path(__file__)
                .parent.joinpath("files/olapps/ceide_metadata.xml")
                .read_text(),
                principal_type="ATTRIBUTE",
                principal_attribute="Email",
                name_id_format=NameIdFormat.unspecified,
                keycloak_url=keycloak_url,
                learn_domain=mitlearn_domain,
                realm_id=ol_apps_realm.id,
                first_login_flow=ol_first_login_flow,
                resource_options=resource_options,
                attribute_map={
                    "email": "Email",
                    "firstName": "Given Name",
                    "lastName": "Surname",
                    "fullName": "Display Name",
                },
            )
        )
        create_org_for_learn(
            OrgConfig(
                org_domains=["ttt-mit.edu"],
                org_name="Train the Trainer",
                org_alias="TTT",
                learn_domain=mitlearn_domain,
                realm_id=ol_apps_realm.id,
                resource_options=resource_options,
            )
        )
        onboard_oidc_org(
            OIDCIdpConfig(
                org_domains=[
                    "duth.gr",
                    "admin.duth.gr",
                    "af.duth.gr",
                    "affil.duth.gr",
                    "agro.duth.gr",
                    "arch.duth.gr",
                    "bscc.duth.gr",
                    "chem.duth.gr",
                    "civil.duth.gr",
                    "cs.duth.gr",
                    "econ.duth.gr",
                    "edu.duth.gr",
                    "ee.duth.gr",
                    "eled.duth.gr",
                    "eng.duth.gr",
                    "env.duth.gr",
                    "fmenr.duth.gr",
                    "he.duth.gr",
                    "helit.duth.gr",
                    "hs.duth.gr",
                    "law.duth.gr",
                    "mbg.duth.gr",
                    "med.duth.gr",
                    "mst.duth.gr",
                    "neclir.duth.gr",
                    "nurs.duth.gr",
                    "ores.duth.gr",
                    "ot.duth.gr",
                    "phyed.duth.gr",
                    "physics.duth.gr",
                    "pme.duth.gr",
                    "polsci.duth.gr",
                    "psed.duth.gr",
                    "psy.duth.gr",
                    "sci.duth.gr",
                    "socadm.duth.gr",
                    "sp.duth.gr",
                    "sw.duth.gr",
                    "vo.duth.gr",
                    "xan.duth.gr",
                ],
                org_name="Democritus University of Thrace",
                org_alias="DUTH",
                learn_domain=mitlearn_domain,
                realm_id=ol_apps_realm.id,
                keycloak_url=keycloak_url,
                first_login_flow=ol_first_login_flow,
                resource_options=resource_options,
                org_oidc_metadata_url="https://sso.duth.gr/realms/main/.well-known/openid-configuration",
                client_id="mit-learn",
            )
        )
        onboard_oidc_org(
            OIDCIdpConfig(
                org_domains=["athenscollege.edu.gr"],
                org_name="Hellenic American Educational Foundation",
                org_alias="HAEF",
                learn_domain=mitlearn_domain,
                realm_id=ol_apps_realm.id,
                keycloak_url=keycloak_url,
                first_login_flow=ol_first_login_flow,
                resource_options=resource_options,
                org_oidc_metadata_url="https://login.microsoftonline.com/35a07f23-c5cb-4b42-81ad-10d269586c9a/v2.0/.well-known/openid-configuration",
                client_id="1b380514-33ff-4dca-a26f-ddd0600b2f02",
            )
        )
        onboard_oidc_org(
            OIDCIdpConfig(
                org_domains=["nust.na"],
                org_name="Namibia University of Science and Technology",
                org_alias="NUST",
                learn_domain=mitlearn_domain,
                realm_id=ol_apps_realm.id,
                keycloak_url=keycloak_url,
                first_login_flow=ol_first_login_flow,
                resource_options=resource_options,
                org_oidc_metadata_url="https://login.microsoftonline.com/d5cf20c2-4a84-4902-a3e6-c4a3190ea239/v2.0/.well-known/openid-configuration",
                client_id="a5c7a2f3-47ce-4e3c-b4ce-fe79b86660b7",
            )
        )
        onboard_oidc_org(
            OIDCIdpConfig(
                org_domains=["upgrad.com"],
                org_name="upGrad",
                org_alias="UPGRAD",
                learn_domain=mitlearn_domain,
                realm_id=ol_apps_realm.id,
                keycloak_url=keycloak_url,
                first_login_flow=ol_first_login_flow,
                resource_options=resource_options,
                org_oidc_metadata_url="https://stage-idp.upgrad.dev/realms/upgrad-stage/.well-known/openid-configuration",
                client_id="ira-frontend",
            )
        )
        onboard_oidc_org(
            OIDCIdpConfig(
                org_domains=["dynideas.com", "veltiston.ai"],
                org_name="Dynamic Ideas, LLC.",
                org_alias="DYNIDEAS",
                learn_domain=mitlearn_domain,
                realm_id=ol_apps_realm.id,
                keycloak_url=keycloak_url,
                first_login_flow=ol_first_login_flow,
                resource_options=resource_options,
                org_oidc_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                client_id="393621505200-fb5rhjqvvmdodn5jdjdv1h1fbpk6rgq7.apps.googleusercontent.com",
                client_secret=keycloak_realm_config.require("dynideas_client_secret"),
            )
        )

    # B2B Organizations [END]

    if stack_info.env_suffix in ["ci", "qa"]:
        # OL-DEV-FAKE-TOUCHSTONE [START] # noqa: ERA001
        ol_apps_dev_fake_touchstone_ci_identity_provider = (
            keycloak.saml.IdentityProvider(
                "fake-touchstone",
                realm=ol_apps_realm.id,
                alias="fake-touchstone",
                display_name="Fake Touchstone",
                entity_id=f"{keycloak_url}/realms/olapps",
                name_id_policy_format="Unspecified",
                force_authn=False,
                post_binding_response=True,
                post_binding_authn_request=True,
                principal_type="SUBJECT",
                single_sign_on_service_url=keycloak_realm_config.get(
                    "fake_touchstone_single_sign_on_service_url"
                ),
                trust_email=True,
                validate_signature=True,
                signing_certificate=keycloak_realm_config.get(
                    "fake_touchstone_sig_cert"
                ),
                want_assertions_encrypted=True,
                want_assertions_signed=True,
                opts=resource_options,
                first_broker_login_flow_alias=ol_first_login_flow.alias,
                hide_on_login_page=False,
                gui_order="60",
            )
        )
        (
            keycloak.AttributeImporterIdentityProviderMapper(
                f"map-fake-touchstone-{stack_info.env_suffix}-saml-email-attribute",
                realm=ol_apps_realm.id,
                attribute_name="email",
                identity_provider_alias=ol_apps_dev_fake_touchstone_ci_identity_provider.alias,
                user_attribute="email",
                extra_config={
                    "syncMode": "INHERIT",
                },
                opts=resource_options,
            ),
        )
        (
            keycloak.AttributeImporterIdentityProviderMapper(
                f"map-fake-touchstone-{stack_info.env_suffix}-saml-last-name-attribute",
                realm=ol_apps_realm.id,
                attribute_name="sn",
                identity_provider_alias=ol_apps_dev_fake_touchstone_ci_identity_provider.alias,
                user_attribute="lastName",
                extra_config={
                    "syncMode": "INHERIT",
                },
                opts=resource_options,
            ),
        )
        keycloak.AttributeImporterIdentityProviderMapper(
            f"map-fake-touchstone-{stack_info.env_suffix}-saml-first-name-attribute",
            realm=ol_apps_realm.id,
            attribute_name="givenName",
            identity_provider_alias=ol_apps_dev_fake_touchstone_ci_identity_provider.alias,
            user_attribute="firstName",
            extra_config={
                "syncMode": "INHERIT",
            },
            opts=resource_options,
        )
        # OL-DEV-FAKE-TOUCHSTONE [END] # noqa: ERA001
        # OKTA-DEV [START] # noqa: ERA001
        ol_apps_okta_saml_identity_provider = keycloak.saml.IdentityProvider(
            "okta-test",
            realm=ol_apps_realm.id,
            alias="okta-test",
            display_name="Okta test",
            post_binding_logout=False,
            post_binding_response=True,
            backchannel_supported=False,
            entity_id=f"{keycloak_url}/realms/olapps",
            authn_context_comparison_type="exact",
            sync_mode="IMPORT",
            single_sign_on_service_url=keycloak_realm_config.get(
                "okta_single_sign_on_service_url"
            ),
            want_assertions_signed=False,
            gui_order="50",
            validate_signature=False,
            hide_on_login_page=False,
            signing_certificate=keycloak_realm_config.get("okta_sig_cert"),
            name_id_policy_format="Email",
            want_assertions_encrypted=False,
            post_binding_authn_request=True,
            force_authn=False,
            principal_type="SUBJECT",
            first_broker_login_flow_alias=ol_first_login_flow.alias,
            opts=resource_options,
        )
        (
            keycloak.AttributeImporterIdentityProviderMapper(
                "map-okta-email-attribute",
                realm=ol_apps_realm.id,
                attribute_name="email",
                identity_provider_alias=ol_apps_okta_saml_identity_provider.alias,
                user_attribute="email",
                extra_config={
                    "syncMode": "INHERIT",
                },
                opts=resource_options,
            ),
        )
        (
            keycloak.AttributeImporterIdentityProviderMapper(
                "map-okta-last-name-attribute",
                realm=ol_apps_realm.id,
                attribute_name="lastName",
                identity_provider_alias=ol_apps_okta_saml_identity_provider.alias,
                user_attribute="lastName",
                extra_config={
                    "syncMode": "INHERIT",
                },
                opts=resource_options,
            ),
        )
        keycloak.AttributeImporterIdentityProviderMapper(
            "map-okta-first-name-attribute",
            realm=ol_apps_realm.id,
            attribute_name="firstName",
            identity_provider_alias=ol_apps_okta_saml_identity_provider.alias,
            user_attribute="firstName",
            extra_config={
                "syncMode": "INHERIT",
            },
            opts=resource_options,
        )
        # OKTA-DEV [END] # noqa: ERA001

    return ol_apps_realm
