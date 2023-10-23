import json
import secrets

import pulumi_keycloak as keycloak
import pulumi_vault as vault
from bridge.lib.magic_numbers import SECONDS_IN_ONE_DAY
from pulumi import Config, Output, ResourceOptions

from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

env_config = Config("environment")
stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
keycloak_config = Config("keycloak")
setup_vault_provider()

# Create a Keycloak provider cause we ran into an issue with pulumi reading
# config from stack definition.
keycloak_url = keycloak_config.require("url")
keycloak_provider = keycloak.Provider(
    "keycloak_provider",
    url=keycloak_url,
    realm="master",
    client_id=keycloak_config.get("client_id"),
    client_secret=keycloak_config.get("client_secret"),
    initial_login=True,
)

resource_options = ResourceOptions(provider=keycloak_provider)
captcha_secret_key = keycloak_config.require("captcha_secret_key")
captcha_site_key = keycloak_config.require("captcha_site_key")
email_host = keycloak_config.require("email_host")
email_password = keycloak_config.require("email_password")
email_username = keycloak_config.require("email_username")
keycloak_url = keycloak_config.get("url")
mit_touchstone_cert = "MIIDCDCCAfCgAwIBAgIJAK/yS5ltGi7MMA0GCSqGSIb3DQEBBQUAMBYxFDASBgNVBAMTC2lkcC5taXQuZWR1MB4XDTEyMDczMDIxNTAxN1oXDTMyMDcyNTIxNTAxN1owFjEUMBIGA1UEAxMLaWRwLm1pdC5lZHUwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQDgC5Y2mM/VMThzTWrZ2uyv3Gw0mWU9NgQpWN1HQ/lLBxH1H6pMc5+fGpOdrvxH/Nepdg6uAJwZrclTDAHHpG/THb7K063NRtic8h9UYSqwxIWUCXI8qNijcWA2bW6PFEy4yIP611J+IzQxzD/ZiR+89ouzdjNBrPHzoaIoMwflftYnFc4L/qu4DxE/NWgANYPGEJfWUFTVpfNV1Iet60904zl+O7T79mwaQwwOMUWwk/DEQyvG6bf2uWL4aFx4laBOekrA+5rSHUXAFlhCreTnzZMkVoxSGqYlc5uZuZmpFCXZn+tNpsVYz+c4Hve3WOZwhx/7bMGCwlx7oovoQWQ5AgMBAAGjWTBXMDYGA1UdEQQvMC2CC2lkcC5taXQuZWR1hh5odHRwczovL2lkcC5taXQuZWR1L3NoaWJib2xldGgwHQYDVR0OBBYEFF5aINzhvMR+pOijYHtr3yCKsrMSMA0GCSqGSIb3DQEBBQUAA4IBAQDfVpscchXXa4Al/l9NGNwQ1shpQ8d+k+NpX2Q976jau9DhVHa42F8bfl1EeHLMFlN79aUxFZb3wvr0h5pq3a8F9aWHyKe+0R10ikVueDcAmg0V7MWthFdsyMwHPbnCdSXo2wh0GhjeIF3f3+hZZwrZ4sZqjX2RmsYnyXgS1r5mzuu4W447Q1fbC5BeZTefUhJcfHQ56ztIFtLJdRuHHnqj09CaQVMD1FtovM86vYwVMwMsgOgkN3c7tW6kXHHBHeEA31xUJsqXGTRlwMSyJTju3SFvhXI/8ZIxshTzWURBo+vf6A6QQvSvJAju4zVLZy83YB/cvAFsV3BexZ4xzuQD"  # pragma: allowlist secret # noqa: E501
okta_test_saml_certificate = "MIIDqDCCApCgAwIBAgIGAYaDoqIJMA0GCSqGSIb3DQEBCwUAMIGUMQswCQYDVQQGEwJVUzETMBEGA1UECAwKQ2FsaWZvcm5pYTEWMBQGA1UEBwwNU2FuIEZyYW5jaXNjbzENMAsGA1UECgwET2t0YTEUMBIGA1UECwwLU1NPUHJvdmlkZXIxFTATBgNVBAMMDGRldi02Njk0MDg0NDEcMBoGCSqGSIb3DQEJARYNaW5mb0Bva3RhLmNvbTAeFw0yMzAyMjQxMzM0MTlaFw0zMzAyMjQxMzM1MThaMIGUMQswCQYDVQQGEwJVUzETMBEGA1UECAwKQ2FsaWZvcm5pYTEWMBQGA1UEBwwNU2FuIEZyYW5jaXNjbzENMAsGA1UECgwET2t0YTEUMBIGA1UECwwLU1NPUHJvdmlkZXIxFTATBgNVBAMMDGRldi02Njk0MDg0NDEcMBoGCSqGSIb3DQEJARYNaW5mb0Bva3RhLmNvbTCCASIwDQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEBAKrZ0Vel22z1r18U1KYt/y8am1JL+iwZItqFMTTwdwFfhXhkHxzzF/wZx07LheD01M7Zs39b3rNVBanzEhiwbg1KwF9xRnd+t6FDF40h6jAWwpjzj3T77PKlpmJfQibfeaMuJWKT2xlrHBx343IO\nYOSIz2E4vMGHPAxdKH9ze/IadTaZqpIhuXWaYBbPA/uPePLeetBBf0/mBJBJSHS9vP6MxZ94WUHMuEQ2gIn8rTIZrevxS6qWahky9AwBOGm2OU0NThqeq0KszVHTdKVuAZIfCtkHaosn48QZ2XqmZvRD6V2AZ5Mb2ClRJbPi12lvH3ds8KqWUUmyjDwS88IkN+sCAwEAATANBgkqhkiG9w0BAQsFAAOCAQEAAllfqAsLw+tPLQTNejbkNfZs6j62PmoKctiGz8xSPVzGedS5qFzLmA5yXSxHOVtIODPlNmlR/ZTaaEg3skXVzsmxygYvcUHKsuhThwXMOdnHu4NiyVyHYtrjp2FyN4YXJcPnOEqjzSTuJEZXbNSIDtZ9QzngeaikibdoKplCRhnp0y3RPVXqRmlSWpOmZ1yE23gZ9oNkdgdtsYh6XfqtNsyt/R8hDHONwwcUD7duNc7UvjXop3GXuBYFUvvLwEScaSTut2e8Mmh+VtRNE2jel7mIU57znw3wJiclQKPkZibX/5mcRZnHw0QH6UReoi19EoutPOV6hw1uSaRQ1KQuPQ=="  # pragma: allowlist secret # noqa: E501
# Create OL Platform Engineering Realm
ol_platform_engineering_realm = keycloak.Realm(
    "ol-platform-engineering",
    access_code_lifespan="30m",
    access_code_lifespan_user_action="15m",
    attributes={
        "business_unit": f"operations-{env_name}",
    },
    display_name="OL PLatform Engineering",
    display_name_html="<b>OL PLatform Engineering</b>",
    enabled=True,
    login_theme="keycloak",
    duplicate_emails_allowed=False,
    otp_policy=keycloak.RealmOtpPolicyArgs(
        algorithm="HmacSHA256",
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
        "upperCase(2) and digits(4) and length(30) and specialChars(4) and"
        " forceExpiredPasswordChange(365) and notUsername and notEmail"
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
            password=email_password,
            username=email_username,
        ),
        from_="odl-devops@mit.edu",
        from_display_name="Identity - OL PLatform Engineering",
        host=email_host,
        port=587,
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

required_action_configure_otp = keycloak.RequiredAction(
    "configure-totp",
    realm_id=ol_platform_engineering_realm.realm,
    alias="CONFIGURE_TOTP",
    default_action=True,
    enabled=True,
    opts=resource_options,
)

required_action_verify_email = keycloak.RequiredAction(
    "verify_email",
    realm_id=ol_platform_engineering_realm.realm,
    alias="VERIFY_EMAIL",
    default_action=True,
    enabled=True,
    opts=resource_options,
)

required_action_update_password = keycloak.RequiredAction(
    "update_password",
    realm_id=ol_platform_engineering_realm.realm,
    alias="UPDATE_PASSWORD",
    default_action=True,
    enabled=True,
    opts=resource_options,
)

# Create OL Public Realm
ol_apps_realm = keycloak.Realm(
    "olapps",
    access_code_lifespan="30m",
    access_code_lifespan_user_action="15m",
    attributes={
        "business_unit": f"operations-{env_name}",
    },
    display_name="OL Apps",
    display_name_html="<b>MIT Open Learning Applications</b>",
    enabled=True,
    login_theme="keycloak",
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
        "upperCase(1) and digits(1) and specialChars(1) and length(8) and notUsername"
        " and notEmail"
    ),
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
            x_robots_tag="none",
            x_xss_protection="1; mode=block",
        ),
    ),
    smtp_server=keycloak.RealmSmtpServerArgs(
        auth=keycloak.RealmSmtpServerAuthArgs(
            password=email_password,
            username=email_username,
        ),
        from_="odl-devops@mit.edu",
        from_display_name="Identity - Open Learning Platform Engineering",
        host=email_host,
        port=587,
        reply_to="odl-devops@mit.edu",
        reply_to_display_name="Identity - Open Learning Platform Engineering",
        starttls=True,
    ),
    ssl_required="external",
    offline_session_idle_timeout="168h",
    sso_session_idle_timeout="2h",
    sso_session_max_lifespan="24h",
    opts=resource_options,
)

olapps_realm_events = keycloak.RealmEvents(
    "realmEvents",
    realm_id=ol_apps_realm.realm,
    events_enabled=True,
    events_expiration=SECONDS_IN_ONE_DAY,
    admin_events_enabled=True,
    admin_events_details_enabled=True,
    events_listeners=["metrics-listener"],
    opts=resource_options,
)

ol_apps_required_action_configure_otp = keycloak.RequiredAction(
    "ol-apps-configure-totp",
    realm_id=ol_apps_realm.realm,
    alias="CONFIGURE_TOTP",
    default_action=False,
    enabled=True,
    opts=resource_options,
)

ol_apps_required_action_verify_email = keycloak.RequiredAction(
    "ol-apps-verify-email",
    realm_id=ol_apps_realm.realm,
    alias="VERIFY_EMAIL",
    default_action=True,
    enabled=True,
    opts=resource_options,
)

ol_apps_required_action_update_password = keycloak.RequiredAction(
    "ol-apps-update-password",
    realm_id=ol_apps_realm.realm,
    alias="UPDATE_PASSWORD",
    default_action=False,
    enabled=True,
    opts=resource_options,
)

# Check if any Openid clients exist in config and create them
for openid_clients in keycloak_config.get_object("openid_clients"):
    realm_name = openid_clients.get("realm_name")
    client_details = openid_clients.get("client_info")
    for client_name, client_url in client_details.items():
        openid_client = keycloak.openid.Client(
            f"{realm_name}-{client_name}-client",
            realm_id=realm_name,
            client_id=f"ol-{client_name}-client",
            enabled=True,
            access_type="CONFIDENTIAL",
            standard_flow_enabled=True,
            valid_redirect_uris=[f"{client_url}/*"],
            login_theme="keycloak",
            opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
        )
        vault.generic.Secret(
            f"{realm_name}-{client_name}-vault-oidc-credentials",
            path=f"secret-operations/sso/{client_name}",
            data_json=Output.all(
                url=openid_client.realm_id.apply(
                    lambda realm_id: f"{keycloak_url}/realms/{realm_id}"
                ),
                client_id=openid_client.client_id,
                client_secret=openid_client.client_secret,
                # This is included for the case where we are using traefik-forward-auth.
                # It requires a random secret value to be present which is independent
                # of the OAuth credentials.
                secret=secrets.token_urlsafe(),
            ).apply(json.dumps),
        )

# OL - First login flow [START]
# Does not require email verification or confirmation to connect with existing account.
ol_touchstone_first_login_flow = keycloak.authentication.Flow(
    "ol-touchstone-first-login-flow",
    realm_id=ol_apps_realm.id,
    alias="ol-first-login-flow",
    opts=resource_options,
)
ol_touchstone_first_login_flow_review_profile = keycloak.authentication.Execution(
    "ol-touchstone-first-login-flow-review-profile",
    realm_id=ol_apps_realm.id,
    parent_flow_alias=ol_touchstone_first_login_flow.alias,
    authenticator="idp-review-profile",
    requirement="REQUIRED",
    opts=resource_options,
)
ol_touchstone_first_login_review_profile_config = (
    keycloak.authentication.ExecutionConfig(
        "ol-touchstone-first-login-review-profile-config",
        realm_id=ol_apps_realm.id,
        execution_id=ol_touchstone_first_login_flow_review_profile.id,
        alias="review-profile-config",
        config={
            "updateProfileOnFirstLogin": "missing",
        },
        opts=resource_options,
    )
)
ol_touchstone_user_creation_or_linking_subflow = keycloak.authentication.Subflow(
    "ol-touchstone-user-creation-or-linking-subflow",
    realm_id=ol_apps_realm.id,
    alias="ol-touchstone-first-broker-login-user-creation-or-linking",
    parent_flow_alias=ol_touchstone_first_login_flow.alias,
    provider_id="basic-flow",
    requirement="REQUIRED",
    opts=resource_options,
)
ol_touchstone_user_creation_or_linking_subflow_create_user_if_unique_step = (
    keycloak.authentication.Execution(
        "ol-touchstone-create-user-if-unique",
        realm_id=ol_apps_realm.id,
        parent_flow_alias=ol_touchstone_user_creation_or_linking_subflow.alias,
        authenticator="idp-create-user-if-unique",
        requirement="ALTERNATIVE",
        opts=resource_options,
    )
)
ol_touchstone_user_creation_or_linking_subflow_automatically_set_existing_user_step = (
    keycloak.authentication.Execution(
        "ol-touchstone-automatically-set-existing-user",
        realm_id=ol_apps_realm.id,
        parent_flow_alias=ol_touchstone_user_creation_or_linking_subflow.alias,
        authenticator="idp-auto-link",
        requirement="ALTERNATIVE",
        opts=resource_options,
    )
)
# OL - First login flow [END]

if stack_info.env_suffix != "ci":
    # Touchstone SAML [START]
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
        first_broker_login_flow_alias=ol_touchstone_first_login_flow.alias,
    )
    oidc_attribute_importer_identity_provider_mapper = (
        keycloak.AttributeImporterIdentityProviderMapper(
            "map-touchstone-saml-email-attribute",
            realm=ol_apps_realm.id,
            attribute_name="email",
            identity_provider_alias=ol_apps_touchstone_saml_identity_provider.alias,
            user_attribute="email",
            extra_config={
                "syncMode": "INHERIT",
            },
            opts=resource_options,
        ),
        keycloak.AttributeImporterIdentityProviderMapper(
            "map-touchstone-saml-last-name-attribute",
            realm=ol_apps_realm.id,
            attribute_name="sn",
            identity_provider_alias=ol_apps_touchstone_saml_identity_provider.alias,
            user_attribute="lastName",
            extra_config={
                "syncMode": "INHERIT",
            },
            opts=resource_options,
        ),
        keycloak.AttributeImporterIdentityProviderMapper(
            "map-touchstone-saml-first-name-attribute",
            realm=ol_apps_realm.id,
            attribute_name="givenName",
            identity_provider_alias=ol_apps_touchstone_saml_identity_provider.alias,
            user_attribute="firstName",
            extra_config={
                "syncMode": "INHERIT",
            },
            opts=resource_options,
        ),
    )
    # Touchstone SAML [END]

if stack_info.env_suffix == "qa":
    # OKTA-DEV-QA [START] # noqa: ERA001
    ol_apps_okta_saml_identity_provider = keycloak.saml.IdentityProvider(
        "okta-test",
        realm=ol_apps_realm.id,
        alias="okta-test",
        post_binding_logout=False,
        post_binding_response=True,
        backchannel_supported=False,
        entity_id=f"{keycloak_url}/realms/olapps",
        login_hint=False,
        authn_context_comparison_type="exact",
        sync_mode="IMPORT",
        single_sign_on_service_url="https://dev-66940844.okta.com/app/dev-66940844_collintestlogin_1/exk8gfblmeePE5uUQ5d7/sso/saml",
        want_assertions_signed=False,
        gui_order="10",
        validate_signature=False,
        hide_on_login_page=True,
        signing_certificate=okta_test_saml_certificate,
        name_id_policy_format="Email",
        want_assertions_encrypted=False,
        post_binding_authn_request=True,
        force_authn=False,
        principal_type="ATTRIBUTE",
        first_broker_login_flow_alias=ol_touchstone_first_login_flow.alias,
        opts=resource_options,
    )
    oidc_attribute_importer_identity_provider_mapper = (
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
        ),
    )
    # OKTA-DEV-QA [END] # noqa: ERA001

if stack_info.env_suffix == "ci":
    # OKTA-DEV-CI [START] # noqa: ERA001
    ol_apps_okta_saml_identity_provider = keycloak.saml.IdentityProvider(
        "okta-test",
        realm=ol_apps_realm.id,
        alias="okta-test",
        post_binding_logout=False,
        post_binding_response=True,
        backchannel_supported=False,
        entity_id=f"{keycloak_url}/realms/olapps",
        login_hint=False,
        authn_context_comparison_type="exact",
        sync_mode="IMPORT",
        single_sign_on_service_url="https://dev-66940844.okta.com/app/dev-66940844_collintestlogin_1/exk8gfblmeePE5uUQ5d7/sso/saml",
        want_assertions_signed=False,
        gui_order="10",
        validate_signature=False,
        hide_on_login_page=False,
        signing_certificate=okta_test_saml_certificate,
        name_id_policy_format="Email",
        want_assertions_encrypted=False,
        post_binding_authn_request=True,
        force_authn=False,
        principal_type="ATTRIBUTE",
        first_broker_login_flow_alias=ol_touchstone_first_login_flow.alias,
        opts=resource_options,
    )
    oidc_attribute_importer_identity_provider_mapper = (
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
        ),
    )
    # OKTA-DEV-CI [END] # noqa: ERA001

if stack_info.env_suffix in ["ci", "qa"]:
    # OL-DEV-FAKE-TOUCHSTONE [START] # noqa: ERA001
    ol_apps_dev_fake_touchstone_ci_identity_provider = keycloak.saml.IdentityProvider(
        "fake-touchstone",
        realm=ol_apps_realm.id,
        alias="fake-touchstone",
        display_name="Fake Touchstone",
        entity_id="http://www.okta.com/exkcta3wbyYMdAMAP5d7",
        name_id_policy_format="Unspecified",
        force_authn=False,
        post_binding_response=True,
        post_binding_authn_request=True,
        principal_type="ATTRIBUTE",
        principal_attribute="urn:oid:1.3.6.1.4.1.5923.1.1.1.6",
        single_sign_on_service_url=keycloak_config.get_object(
            "fake_touchstone_single_sign_on_service_url"
        ),
        trust_email=True,
        validate_signature=True,
        signing_certificate=keycloak_config.get_object("fake_touchstone_sig_cert"),
        want_assertions_encrypted=True,
        want_assertions_signed=True,
        opts=resource_options,
        first_broker_login_flow_alias=ol_touchstone_first_login_flow.alias,
        hide_on_login_page=False,
    )
    oidc_attribute_importer_identity_provider_mapper = (
        keycloak.AttributeImporterIdentityProviderMapper(
            "map-fake-touchstone-ci-saml-email-attribute",
            realm=ol_apps_realm.id,
            attribute_name="email",
            identity_provider_alias=ol_apps_dev_fake_touchstone_ci_identity_provider.alias,
            user_attribute="email",
            extra_config={
                "syncMode": "INHERIT",
            },
            opts=resource_options,
        ),
        keycloak.AttributeImporterIdentityProviderMapper(
            "map-fake-touchstone-ci-saml-last-name-attribute",
            realm=ol_apps_realm.id,
            attribute_name="sn",
            identity_provider_alias=ol_apps_dev_fake_touchstone_ci_identity_provider.alias,
            user_attribute="lastName",
            extra_config={
                "syncMode": "INHERIT",
            },
            opts=resource_options,
        ),
        keycloak.AttributeImporterIdentityProviderMapper(
            "map-fake-touchstone-ci-saml-first-name-attribute",
            realm=ol_apps_realm.id,
            attribute_name="givenName",
            identity_provider_alias=ol_apps_dev_fake_touchstone_ci_identity_provider.alias,
            user_attribute="firstName",
            extra_config={
                "syncMode": "INHERIT",
            },
            opts=resource_options,
        ),
    )
    # OL-DEV-FAKE-TOUCHSTONE-CI [END] # noqa: ERA001
