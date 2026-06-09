"""
Keycloak olapps realm for local development.

Mirrors the production olapps.py structure but:
  - Skips Vault secrets (stores OIDC credentials as plain k8s Secrets instead)
  - Skips production SAML/OIDC org federation (real MIT Touchstone, B2B orgs)
  - Disables verify_email (Mailpit is available but we want frictionless login)
  - Includes fake-touchstone and okta-test IdPs (same as CI/QA branch)
  - Adds test users: admin@odl.local, student@odl.local, prof@odl.local
  - Skips all Vault-dependent resources
"""

import json

import pulumi_keycloak as keycloak
import pulumi_kubernetes as k8s
from pulumi import InvokeOptions, Output, ResourceOptions

from ol_infrastructure.substructure.keycloak.org_flows import (
    create_organization_browser_flows,
    create_organization_first_broker_login_flows,
)


def create_olapps_dev_realm(  # noqa: PLR0913
    keycloak_provider: keycloak.Provider,
    keycloak_url: str,
    k8s_provider: k8s.Provider,
    mitlearn_client_secret: Output,
    learn_ai_client_secret: Output,
    mitxonline_client_secret: Output,
    unified_ecommerce_client_secret: Output,
) -> None:
    """
    Create the olapps Keycloak realm for local development.

    Provisions all clients and stores OIDC credentials as plain k8s Secrets
    (no Vault dependency). The realm mirrors production configuration but omits
    production-only IdPs and disables email verification.
    """
    kc_opts = ResourceOptions(provider=keycloak_provider)
    k8s_opts = ResourceOptions(provider=k8s_provider)

    # -------------------------------------------------------------------------
    # Realm
    # -------------------------------------------------------------------------

    realm = keycloak.Realm(
        "olapps",
        access_code_lifespan="30m",
        access_code_lifespan_user_action="15m",
        attributes={"business_unit": "operations-local"},
        display_name="MIT Learn",
        display_name_html="<b>MIT Learn</b>",
        enabled=True,
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
        # Disabled for local dev — avoids email verification friction.
        verify_email=False,
        password_policy=(  # pragma: allowlist secret  # noqa: S106
            "length(8) and notUsername and notEmail"
        ),
        security_defenses=keycloak.RealmSecurityDefensesArgs(
            brute_force_detection=keycloak.RealmSecurityDefensesBruteForceDetectionArgs(
                failure_reset_time_seconds=43200,
                max_failure_wait_seconds=3600,
                max_login_failures=10,
                max_temporary_lockouts=1,
                minimum_quick_login_wait_seconds=60,
                permanent_lockout=True,
                quick_login_check_milli_seconds=700,
                wait_increment_seconds=300,
            ),
            headers=keycloak.RealmSecurityDefensesHeadersArgs(
                content_security_policy=(
                    "frame-src 'self'; frame-ancestors 'self'; object-src 'none';"
                ),
                content_security_policy_report_only="",
                strict_transport_security="max-age=31536000; includeSubDomains",
                x_content_type_options="nosniff",
                x_frame_options="SAMEORIGIN",
                x_robots_tag="none",
                x_xss_protection="1; mode=block",
            ),
        ),
        # Use Mailpit for local SMTP. No auth needed — omitting auth block
        # entirely avoids a provider panic when empty credentials are passed.
        smtp_server=keycloak.RealmSmtpServerArgs(
            from_="noreply@mit.dev",
            from_display_name="MIT Learn Local",
            host="mailpit.local-infra.svc.cluster.local",
            port="1025",
            ssl=False,
            starttls=False,
        ),
        ssl_required="external",
        offline_session_idle_timeout="168h",
        organizations_enabled=True,
        sso_session_idle_timeout="336h",
        sso_session_max_lifespan="336h",
        opts=kc_opts,
    )

    keycloak.RealmEvents(
        "realmEvents",
        realm_id=realm.realm,
        events_enabled=True,
        admin_events_enabled=True,
        admin_events_details_enabled=True,
        events_listeners=["jboss-logging"],
        opts=kc_opts,
    )

    for alias, default in [
        ("CONFIGURE_TOTP", False),
        ("VERIFY_EMAIL", False),  # disabled for local dev
        # UPDATE_EMAIL was removed in Keycloak 26 — omit to avoid validation error.
        ("UPDATE_PASSWORD", False),
    ]:
        keycloak.RequiredAction(
            f"ol-apps-{alias.lower().replace('_', '-')}",
            realm_id=realm.realm,
            alias=alias,
            default_action=default,
            enabled=True,
            opts=kc_opts,
        )

    # -------------------------------------------------------------------------
    # User Profile (mirrors production)
    # -------------------------------------------------------------------------

    keycloak.RealmUserProfile(
        "olapps-user-profile",
        realm_id=realm.realm,
        attributes=[
            keycloak.RealmUserProfileAttributeArgs(
                name="fullName",
                display_name="${fullName}",
                validators=[
                    keycloak.RealmUserProfileAttributeValidatorArgs(
                        name="length", config={"max": "512"}
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
                        name="email", config={}
                    ),
                    keycloak.RealmUserProfileAttributeValidatorArgs(
                        name="length", config={"max": "255"}
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
                        name="length", config={"min": "3", "max": "255"}
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
                        name="length", config={"max": "255"}
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
                        name="length", config={"max": "255"}
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
        opts=kc_opts,
    )

    # -------------------------------------------------------------------------
    # ol-profile client scope + attribute mappers
    # -------------------------------------------------------------------------

    ol_profile_scope = keycloak.openid.ClientScope(
        "ol-profile-client-scope",
        name="ol-profile",
        realm_id=realm.realm,
        opts=kc_opts,
    )

    keycloak.openid.UserAttributeProtocolMapper(
        "email-optin-mapper",
        realm_id=realm.realm,
        client_scope_id=ol_profile_scope.id,
        name="email-optin-mapper",
        user_attribute="emailOptIn",
        claim_name="email_optin",
        opts=kc_opts,
    )
    keycloak.openid.UserAttributeProtocolMapper(
        "fullname-mapper",
        realm_id=realm.realm,
        client_scope_id=ol_profile_scope.id,
        name="fullname-mapper",
        user_attribute="fullName",
        claim_name="name",
        opts=kc_opts,
    )

    # -------------------------------------------------------------------------
    # Auth flows (same as production)
    # -------------------------------------------------------------------------

    ol_first_login_flow = create_organization_first_broker_login_flows(
        realm.realm, "olapps", opts=kc_opts
    )
    ol_browser_flow = create_organization_browser_flows(
        realm.realm, "olapps", opts=kc_opts
    )

    keycloak.authentication.Bindings(
        "ol-apps-flow-bindings",
        realm_id=realm.realm,
        browser_flow=ol_browser_flow.alias,
        first_broker_login_flow=ol_first_login_flow.alias,
        opts=kc_opts,
    )

    # Keycloak 26 auto-creates the "organization" client scope (and its mapper)
    # when organizations_enabled=True on the realm. Skip create_organization_scope
    # to avoid a 409 Conflict on the first pulumi apply.

    # -------------------------------------------------------------------------
    # Clients + k8s Secrets (replacing Vault secrets)
    # -------------------------------------------------------------------------

    DEFAULT_SCOPES = [
        "acr",
        "email",
        "profile",
        "role_list",
        "roles",
        "web-origins",
        "ol-profile",
        # KC 26 auto-attaches "organization" as an optional scope when
        # organizations_enabled=True — adding it as default causes 409 Conflict.
    ]

    def _make_oidc_secret(
        resource_name: str,
        namespace: str,
        secret_name: str,
        client: keycloak.openid.Client,
        client_secret_value: Output,
    ) -> k8s.core.v1.Secret:
        """Create a k8s Secret with OIDC credentials for APISIX secretRef.

        Only client_id and client_secret are stored here; session.secret is
        set inline in each ApisixRoute config so that APISIX does not treat it
        as a plugin config key during secretRef merging (which would override
        the JWT verification key and break RS256 token validation).
        """
        return k8s.core.v1.Secret(
            resource_name,
            metadata={"name": secret_name, "namespace": namespace},
            string_data=Output.all(
                client_id=client.client_id,
                client_secret=client_secret_value,
                realm_id=client.realm_id,
                realm_name="olapps",
                url=client.realm_id.apply(lambda rid: f"{keycloak_url}/realms/{rid}"),
            ).apply(
                lambda args: {
                    "client_id": args["client_id"],
                    "client_secret": args["client_secret"],
                    "realm_id": args["realm_id"],
                    "realm_name": args["realm_name"],
                    "url": args["url"],
                    # Convenience composite JSON for apps that read it as one value.
                    "oidc_credentials": json.dumps(
                        {
                            "client_id": args["client_id"],
                            "client_secret": args["client_secret"],
                            "realm_id": args["realm_id"],
                            "realm_name": args["realm_name"],
                            "url": args["url"],
                        }
                    ),
                }
            ),
            opts=k8s_opts,
        )

    # --- Unified Ecommerce ---
    ue_client = keycloak.openid.Client(
        "olapps-unified-ecommerce-client",
        name="ol-unified-ecommerce-client",
        realm_id=realm.realm,
        client_id="ol-unified-ecommerce-client",
        client_secret=unified_ecommerce_client_secret,
        enabled=True,
        access_type="CONFIDENTIAL",
        standard_flow_enabled=True,
        implicit_flow_enabled=False,
        service_accounts_enabled=False,
        valid_redirect_uris=["https://unified-ecommerce.mit.dev/*"],
        valid_post_logout_redirect_uris=["https://unified-ecommerce.mit.dev/*"],
        opts=kc_opts.merge(ResourceOptions(delete_before_replace=True)),
    )
    keycloak.openid.ClientDefaultScopes(
        "olapps-ue-default-scopes",
        realm_id=realm.realm,
        client_id=ue_client.id,
        default_scopes=DEFAULT_SCOPES,
        opts=kc_opts,
    )
    _make_oidc_secret(
        "oidc-secret-unified-ecommerce",
        "local-infra",
        "ol-unified-ecommerce-oidc",
        ue_client,
        unified_ecommerce_client_secret,
    )

    # --- Learn AI ---
    learn_ai_client = keycloak.openid.Client(
        "olapps-learn-ai-client",
        name="ol-learn-ai-client",
        realm_id=realm.realm,
        client_id="ol-learn-ai-client",
        client_secret=learn_ai_client_secret,
        enabled=True,
        access_type="CONFIDENTIAL",
        standard_flow_enabled=True,
        implicit_flow_enabled=False,
        service_accounts_enabled=False,
        valid_redirect_uris=[
            "https://ai.learn.mit.dev/*",
        ],
        opts=kc_opts.merge(ResourceOptions(delete_before_replace=True)),
    )
    keycloak.openid.ClientDefaultScopes(
        "olapps-learn-ai-default-scopes",
        realm_id=realm.realm,
        client_id=learn_ai_client.id,
        default_scopes=DEFAULT_SCOPES,
        opts=kc_opts,
    )
    _make_oidc_secret(
        "oidc-secret-learn-ai",
        "learn-ai",
        "ol-learn-ai-oidc",
        learn_ai_client,
        learn_ai_client_secret,
    )

    # --- MIT Learn ---
    mitlearn_client = keycloak.openid.Client(
        "olapps-mitlearn-client",
        name="ol-mitlearn-client",
        realm_id=realm.realm,
        client_id="ol-mitlearn-client",
        client_secret=mitlearn_client_secret,
        enabled=True,
        access_type="CONFIDENTIAL",
        standard_flow_enabled=True,
        implicit_flow_enabled=False,
        service_accounts_enabled=False,
        valid_redirect_uris=[
            "https://learn.mit.dev/*",
            "https://api.learn.mit.dev/*",
        ],
        web_origins=["+"],
        opts=kc_opts.merge(ResourceOptions(delete_before_replace=True)),
    )
    keycloak.openid.ClientDefaultScopes(
        "olapps-mitlearn-default-scopes",
        realm_id=realm.realm,
        client_id=mitlearn_client.id,
        default_scopes=DEFAULT_SCOPES,
        opts=kc_opts,
    )
    _make_oidc_secret(
        "oidc-secret-mitlearn",
        "mit-learn",
        "ol-mitlearn-oidc",
        mitlearn_client,
        mitlearn_client_secret,
    )

    # --- MITx Online ---
    mitxonline_client = keycloak.openid.Client(
        "olapps-mitxonline-client",
        name="ol-mitxonline-client",
        realm_id=realm.realm,
        client_id="ol-mitxonline-client",
        client_secret=mitxonline_client_secret,
        enabled=True,
        access_type="CONFIDENTIAL",
        standard_flow_enabled=True,
        implicit_flow_enabled=False,
        service_accounts_enabled=True,
        valid_redirect_uris=[
            "https://mitxonline.mit.dev/*",
            "https://api.mitxonline.mit.dev/*",
        ],
        opts=kc_opts.merge(ResourceOptions(delete_before_replace=True)),
    )
    keycloak.openid.ClientDefaultScopes(
        "olapps-mitxonline-default-scopes",
        realm_id=realm.realm,
        client_id=mitxonline_client.id,
        default_scopes=DEFAULT_SCOPES,
        opts=kc_opts,
    )
    _make_oidc_secret(
        "oidc-secret-mitxonline",
        "mitxonline",
        "ol-mitxonline-oidc",
        mitxonline_client,
        mitxonline_client_secret,
    )

    # --- MITx Online B2B (service account client for Keycloak Admin API) ---
    mitxonline_b2b_client = keycloak.openid.Client(
        "olapps-mitxonline-b2b-client",
        name="mitxonline-b2b-client",
        realm_id=realm.realm,
        client_id="mitxonline-b2b-client",
        enabled=True,
        access_type="CONFIDENTIAL",
        standard_flow_enabled=False,
        implicit_flow_enabled=False,
        direct_access_grants_enabled=False,
        service_accounts_enabled=True,
        valid_redirect_uris=[],
        opts=kc_opts.merge(ResourceOptions(delete_before_replace=True)),
    )

    realm_management_client = keycloak.openid.get_client_output(
        realm_id=realm.realm,
        client_id="realm-management",
        opts=InvokeOptions(provider=keycloak_provider),
    )
    for resource_name, role in [
        ("b2b-view-realm", "view-realm"),
        ("b2b-view-users", "view-users"),
        ("b2b-query-users", "query-users"),
        ("b2b-manage-realm", "manage-realm"),
    ]:
        keycloak.openid.ClientServiceAccountRole(
            f"olapps-mitxonline-{resource_name}",
            realm_id=realm.realm,
            service_account_user_id=mitxonline_b2b_client.service_account_user_id,
            client_id=realm_management_client.id,
            role=role,
            opts=kc_opts,
        )

    k8s.core.v1.Secret(
        "oidc-secret-mitxonline-b2b",
        metadata={"name": "ol-mitxonline-b2b-oidc", "namespace": "mitxonline"},
        string_data=Output.all(
            client_id=mitxonline_b2b_client.client_id,
            client_secret=mitxonline_b2b_client.client_secret,
            realm_id=mitxonline_b2b_client.realm_id,
        ).apply(
            lambda args: {
                "client_id": args["client_id"],
                "client_secret": args["client_secret"],
                "realm_id": args["realm_id"],
                "realm_name": "olapps",
                "url": f"{keycloak_url}/realms/{args['realm_id']}",
            }
        ),
        opts=k8s_opts,
    )

    # -------------------------------------------------------------------------
    # Fake-Touchstone IdP (CI/QA pattern — skip real Touchstone for local dev)
    # -------------------------------------------------------------------------

    fake_touchstone = keycloak.saml.IdentityProvider(
        "fake-touchstone",
        realm=realm.realm,
        alias="fake-touchstone",
        display_name="Fake Touchstone",
        entity_id=f"{keycloak_url}/realms/olapps",
        name_id_policy_format="Unspecified",
        force_authn=False,
        post_binding_response=True,
        post_binding_authn_request=True,
        principal_type="SUBJECT",
        # For local dev, this can be left empty; it needs to point to a running
        # fake-touchstone service if SAML IdP testing is needed.
        single_sign_on_service_url=f"{keycloak_url}/realms/olapps/protocol/saml",
        trust_email=True,
        validate_signature=False,
        want_assertions_encrypted=False,
        want_assertions_signed=False,
        first_broker_login_flow_alias=ol_first_login_flow.alias,
        hide_on_login_page=False,
        gui_order="60",
        opts=kc_opts,
    )

    for mapper_name, attr_name, user_attr in [
        ("email", "email", "email"),
        ("last-name", "sn", "lastName"),
        ("first-name", "givenName", "firstName"),
    ]:
        keycloak.AttributeImporterIdentityProviderMapper(
            f"fake-touchstone-{mapper_name}",
            realm=realm.realm,
            attribute_name=attr_name,
            identity_provider_alias=fake_touchstone.alias,
            user_attribute=user_attr,
            extra_config={"syncMode": "INHERIT"},
            opts=kc_opts,
        )
