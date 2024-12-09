import json
import secrets
import urllib.request
from functools import partial

import pulumi_keycloak as keycloak
import pulumi_vault as vault
from pulumi import Config, Output, ResourceOptions

from bridge.lib.magic_numbers import SECONDS_IN_ONE_DAY
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

env_config = Config("environment")
stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
keycloak_config = Config("keycloak")
keycloak_realm_config = Config("keycloak_realm")
setup_vault_provider()


def fetch_realm_public_key(keycloak_url: str, realm_id: str) -> str:
    with urllib.request.urlopen(f"{keycloak_url}/realms/{realm_id}/") as response:  # noqa: S310
        public_key_url_response = json.load(response)
    public_key = public_key_url_response["public_key"]
    if public_key:
        pem_lines = [
            "-----BEGIN PUBLIC KEY-----",
            public_key,
            "-----END PUBLIC KEY-----",
        ]
        cert_pem = "\n".join(pem_lines)
    else:
        cert_pem = "No public key found"
    return cert_pem


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
captcha_secret_key = keycloak_realm_config.require("captcha_secret_key")
captcha_site_key = keycloak_realm_config.require("captcha_site_key")
mit_email_host = keycloak_realm_config.require("mit_email_host")
mit_email_password = keycloak_realm_config.require("mit_email_password")
mit_email_username = keycloak_realm_config.require("mit_email_username")
mailgun_email_host = keycloak_realm_config.require("mailgun_email_host")
mailgun_email_password = keycloak_realm_config.require("mailgun_email_password")
mailgun_email_username = keycloak_realm_config.require("mailgun_email_username")
mailgun_reply_to_address = keycloak_realm_config.require("mailgun_reply_to_address")
mit_touchstone_cert = "MIIDCDCCAfCgAwIBAgIJAK/yS5ltGi7MMA0GCSqGSIb3DQEBBQUAMBYxFDASBgNVBAMTC2lkcC5taXQuZWR1MB4XDTEyMDczMDIxNTAxN1oXDTMyMDcyNTIxNTAxN1owFjEUMBIGA1UEAxMLaWRwLm1pdC5lZHUwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQDgC5Y2mM/VMThzTWrZ2uyv3Gw0mWU9NgQpWN1HQ/lLBxH1H6pMc5+fGpOdrvxH/Nepdg6uAJwZrclTDAHHpG/THb7K063NRtic8h9UYSqwxIWUCXI8qNijcWA2bW6PFEy4yIP611J+IzQxzD/ZiR+89ouzdjNBrPHzoaIoMwflftYnFc4L/qu4DxE/NWgANYPGEJfWUFTVpfNV1Iet60904zl+O7T79mwaQwwOMUWwk/DEQyvG6bf2uWL4aFx4laBOekrA+5rSHUXAFlhCreTnzZMkVoxSGqYlc5uZuZmpFCXZn+tNpsVYz+c4Hve3WOZwhx/7bMGCwlx7oovoQWQ5AgMBAAGjWTBXMDYGA1UdEQQvMC2CC2lkcC5taXQuZWR1hh5odHRwczovL2lkcC5taXQuZWR1L3NoaWJib2xldGgwHQYDVR0OBBYEFF5aINzhvMR+pOijYHtr3yCKsrMSMA0GCSqGSIb3DQEBBQUAA4IBAQDfVpscchXXa4Al/l9NGNwQ1shpQ8d+k+NpX2Q976jau9DhVHa42F8bfl1EeHLMFlN79aUxFZb3wvr0h5pq3a8F9aWHyKe+0R10ikVueDcAmg0V7MWthFdsyMwHPbnCdSXo2wh0GhjeIF3f3+hZZwrZ4sZqjX2RmsYnyXgS1r5mzuu4W447Q1fbC5BeZTefUhJcfHQ56ztIFtLJdRuHHnqj09CaQVMD1FtovM86vYwVMwMsgOgkN3c7tW6kXHHBHeEA31xUJsqXGTRlwMSyJTju3SFvhXI/8ZIxshTzWURBo+vf6A6QQvSvJAju4zVLZy83YB/cvAFsV3BexZ4xzuQD"  # pragma: allowlist secret # noqa: E501
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
        "length(30) and forceExpiredPasswordChange(365)"
        "  and notUsername and notEmail"
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
        from_display_name="Identity - OL PLatform Engineering",
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

""" # noqa: ERA001
# This can be uncommented when a new release of the pulumi-keycloak
# library is released that includes the below or similar PR
# https://github.com/mrparkers/terraform-provider-keycloak/pull/858
# Currently this is being done manually in the Keycloak UI.
ol_platform_engineering_rsa_key = keycloak.RealmKeystoreRsa(
    "ol-platform-engineering-rsa-key",
    name="ol-platform-engineering-rsa-key",
    realm_id=ol_platform_engineering_realm.realm,
    certificate="",
    private_key="",
    algorithm="RSA-OEAP",
    opts=resource_options,
)
"""

# Create OL Public Realm
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
    login_theme="ol",
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
            max_failure_wait_seconds=900,
            max_login_failures=10,
            minimum_quick_login_wait_seconds=60,
            permanent_lockout=True,
            quick_login_check_milli_seconds=1000,
            wait_increment_seconds=60,
        ),
        headers=keycloak.RealmSecurityDefensesHeadersArgs(
            content_security_policy=(
                "frame-src 'self' https://www.google.com; frame-ancestors 'self'; object-src 'none';"  # noqa: E501
            ),
            content_security_policy_report_only="",
            strict_transport_security="max-age=31536000; includeSubDomains",
            x_content_type_options="nosniff",
            x_frame_options="https://www.google.com",
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

ol_apps_user_profile = keycloak.RealmUserProfile(
    "olapps-user-profile",
    realm_id=ol_apps_realm.realm,
    attributes=[
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
"""
ol_apps_user_email_optin_attribute_mapper = keycloak.openid.UserAttributeProtocolMapper(
    "email-optin-mapper",
    realm_id=ol_apps_realm.id,
    client_scope_id=ol_apps_profile_client_scope.id,
    name="email-optin-mapper",
    user_attribute="emailOptIn",
    claim_name="email_optin",
)
"""
ol_apps_user_fullname_attribute_mapper = keycloak.openid.UserAttributeProtocolMapper(
    "fullname-mapper",
    realm_id=ol_apps_realm.id,
    client_scope_id=ol_apps_profile_client_scope.id,
    name="fullname-mapper",
    user_attribute="fullName",
    claim_name="name",
)

""" # noqa: ERA001
# This can be uncommented when a new release of the pulumi-keycloak
# library is released that includes the below or similar PR
# https://github.com/mrparkers/terraform-provider-keycloak/pull/858
# Currently this is being done manually in the Keycloak UI.
ol_platform_engineering_rsa_key = keycloak.RealmKeystoreRsa(
    "ol-platform-engineering-rsa-key",
    name="ol-platform-engineering-rsa-key",
    realm_id=ol_platform_engineering_realm.realm,
    certificate="",
    private_key="",
    algorithm="RSA-OEAP",
    opts=resource_options,
)
"""

# Create OL Data Platform Realm
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
    login_theme="keycloak",
    duplicate_emails_allowed=False,
    realm="ol-data-platform",
    reset_password_allowed=False,
    verify_email=False,
    registration_allowed=False,
    password_policy=(  # noqa: S106 # pragma: allowlist secret
        "length(16) and forceExpiredPasswordChange(365)"
        "  and notUsername and notEmail"
    ),
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
    sso_session_idle_timeout="2h",
    sso_session_max_lifespan="24h",
    opts=resource_options,
)

# OL Data - First login flow [START]
# Does not require email verification or confirmation to connect with existing account.
ol_data_touchstone_first_login_flow = keycloak.authentication.Flow(
    "ol-data-touchstone-first-login-flow",
    realm_id=ol_data_platform_realm.id,
    alias="ol-data-first-login-flow",
    opts=resource_options,
)
ol_data_touchstone_first_login_flow_review_profile = keycloak.authentication.Execution(
    "ol-data-touchstone-first-login-flow-review-profile",
    realm_id=ol_data_platform_realm.id,
    parent_flow_alias=ol_data_touchstone_first_login_flow.alias,
    authenticator="idp-review-profile",
    requirement="REQUIRED",
    opts=resource_options,
)
ol_data_touchstone_first_login_review_profile_config = (
    keycloak.authentication.ExecutionConfig(
        "ol-data-touchstone-first-login-review-profile-config",
        realm_id=ol_data_platform_realm.id,
        execution_id=ol_data_touchstone_first_login_flow_review_profile.id,
        alias="review-profile-config",
        config={
            "updateProfileOnFirstLogin": "missing",
        },
        opts=resource_options,
    )
)
ol_data_touchstone_user_creation_or_linking_subflow = keycloak.authentication.Subflow(
    "ol-data-touchstone-user-creation-or-linking-subflow",
    realm_id=ol_data_platform_realm.id,
    alias="ol-data-touchstone-first-broker-login-user-creation-or-linking",
    parent_flow_alias=ol_data_touchstone_first_login_flow.alias,
    provider_id="basic-flow",
    requirement="REQUIRED",
    opts=resource_options,
)
ol_data_touchstone_user_creation_or_linking_subflow_create_user_if_unique_step = (
    keycloak.authentication.Execution(
        "ol-data-touchstone-create-user-if-unique",
        realm_id=ol_data_platform_realm.id,
        parent_flow_alias=ol_data_touchstone_user_creation_or_linking_subflow.alias,
        authenticator="idp-create-user-if-unique",
        requirement="ALTERNATIVE",
        opts=resource_options,
    )
)
ol_data_touchstone_user_creation_or_linking_subflow_automatically_set_existing_user_step = keycloak.authentication.Execution(  # noqa: E501
    "ol-data-touchstone-automatically-set-existing-user",
    realm_id=ol_data_platform_realm.id,
    parent_flow_alias=ol_data_touchstone_user_creation_or_linking_subflow.alias,
    authenticator="idp-auto-link",
    requirement="ALTERNATIVE",
    opts=ResourceOptions(
        provider=keycloak_provider,
        depends_on=ol_data_touchstone_user_creation_or_linking_subflow_create_user_if_unique_step,
    ),
)
# OL - First login flow [END]

# OL Data - Touchstone SAML
ol_data_platform_touchstone_saml_identity_provider = keycloak.saml.IdentityProvider(
    "ol-data-touchstone-idp",
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
    first_broker_login_flow_alias=ol_data_touchstone_first_login_flow.alias,
    opts=resource_options,
)

ol_data_oidc_attribute_importer_identity_provider_mapper = (
    keycloak.AttributeImporterIdentityProviderMapper(
        "ol-data-map-touchstone-saml-email-attribute",
        realm=ol_data_platform_realm.id,
        attribute_name="mail",
        identity_provider_alias=ol_data_platform_touchstone_saml_identity_provider.alias,
        user_attribute="email",
        extra_config={
            "syncMode": "INHERIT",
        },
        opts=resource_options,
    ),
    keycloak.AttributeImporterIdentityProviderMapper(
        "ol-data-map-touchstone-saml-last-name-attribute",
        realm=ol_data_platform_realm.id,
        attribute_name="sn",
        identity_provider_alias=ol_data_platform_touchstone_saml_identity_provider.alias,
        user_attribute="lastName",
        extra_config={
            "syncMode": "INHERIT",
        },
        opts=resource_options,
    ),
    keycloak.AttributeImporterIdentityProviderMapper(
        "ol-data-map-touchstone-saml-first-name-attribute",
        realm=ol_data_platform_realm.id,
        attribute_name="givenName",
        identity_provider_alias=ol_data_platform_touchstone_saml_identity_provider.alias,
        user_attribute="firstName",
        extra_config={
            "syncMode": "INHERIT",
        },
        opts=resource_options,
    ),
    keycloak.UserTemplateImporterIdentityProviderMapper(
        "ol-data-map-touchstone-saml-username-attribute",
        name="username-formatter",
        realm=ol_data_platform_realm.id,
        identity_provider_alias=ol_data_platform_touchstone_saml_identity_provider.alias,
        template="${ATTRIBUTE.mail | localpart}",
    ),
    # Map Moira group membership to superset role
    # ol-eng-data -> superset_admin
    keycloak.AttributeToRoleIdentityMapper(
        "ol-data-saml-superset-admin-ol-eng-data",
        realm=ol_data_platform_realm.id,
        attribute_friendly_name="mitMoiraMemberOf",
        identity_provider_alias=ol_data_platform_touchstone_saml_identity_provider.alias,
        attribute_value="ol-eng-data",
        role="ol-superset-client.superset_admin",
        extra_config={
            "syncMode": "FORCE",
        },
        opts=resource_options,
    ),
    # Map Moira group membership to superset role
    # ol-eng-developer -> superset_alpha
    keycloak.AttributeToRoleIdentityMapper(
        "ol-data-saml-superset-alpha-ol-eng-developer",
        realm=ol_data_platform_realm.id,
        attribute_friendly_name="mitMoiraMemberOf",
        identity_provider_alias=ol_data_platform_touchstone_saml_identity_provider.alias,
        attribute_value="ol-eng-developer",
        role="ol-superset-client.superset_alpha",
        extra_config={
            "syncMode": "FORCE",
        },
        opts=resource_options,
    ),
    # Map Moira group membership to superset role
    # ol-eng-reporter -> superset_gamma
    keycloak.AttributeToRoleIdentityMapper(
        "ol-data-saml-superset-gamma-ol-eng-reporter",
        realm=ol_data_platform_realm.id,
        attribute_friendly_name="mitMoiraMemberOf",
        identity_provider_alias=ol_data_platform_touchstone_saml_identity_provider.alias,
        attribute_value="ol-eng-reporter",
        role="ol-superset-client.superset_gamma",
        extra_config={
            "syncMode": "FORCE",
        },
        opts=resource_options,
    ),
)

fetch_realm_public_key_partial = partial(
    fetch_realm_public_key,
    keycloak_url,
)

# Check if any Openid clients exist in config and create them
for openid_clients in keycloak_realm_config.get_object("openid_clients"):
    realm_name = openid_clients.get("realm_name")
    client_details = openid_clients.get("client_info")
    for client_name, client_detail in client_details.items():
        urls = [url for url in client_detail if url.startswith("http")]

        openid_client = keycloak.openid.Client(
            f"{realm_name}-{client_name}-client",
            name=f"ol-{client_name}-client",
            realm_id=realm_name,
            client_id=f"ol-{client_name}-client",
            enabled=True,
            access_type="CONFIDENTIAL",
            standard_flow_enabled=openid_clients.get("standard_flow_enabled") or True,
            implicit_flow_enabled=openid_clients.get("implicit_flow_enabled") or False,
            pkce_code_challenge_method="S256",
            service_accounts_enabled=openid_clients.get("service_accounts_enabled")
            or False,
            valid_redirect_uris=urls,
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
                realm_id=openid_client.realm_id,
                realm_name=realm_name,
                realm_public_key=openid_client.realm_id.apply(
                    lambda realm_id: fetch_realm_public_key_partial(realm_id)
                ),
            ).apply(json.dumps),
        )
        for role in client_detail[1:]:
            openid_client_role = keycloak.Role(
                role,
                name=role,
                client_id=openid_client.id,
                realm_id=realm_name,
                opts=resource_options,
            )
        if "extra_default_scopes" in openid_clients:
            keycloak.openid.ClientDefaultScopes(
                f"{realm_name}-{client_name}-default-scopes",
                realm_id=realm_name,
                client_id=openid_client.id,
                default_scopes=[
                    "acr",
                    "email",
                    "profile",
                    "role_list",
                    "roles",
                    "web-origins",
                    *openid_clients.get("extra_default_scopes"),
                ],
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
ol_touchstone_user_creation_or_linking_subflow_automatically_set_existing_user_step = keycloak.authentication.Execution(  # noqa: E501
    "ol-touchstone-automatically-set-existing-user",
    realm_id=ol_apps_realm.id,
    parent_flow_alias=ol_touchstone_user_creation_or_linking_subflow.alias,
    authenticator="idp-auto-link",
    requirement="ALTERNATIVE",
    opts=ResourceOptions(
        provider=keycloak_provider,
        depends_on=ol_touchstone_user_creation_or_linking_subflow_create_user_if_unique_step,
    ),
)
# OL - First login flow [END]

"""
# This can be uncommented once this issue is resolved - https://github.com/mrparkers/terraform-provider-keycloak/issues/896
# noqa: ERA001
# OL - Registration flow [START]
ol_registration_flow = keycloak.authentication.Flow(
    "ol-registration-flow",
    realm_id=ol_apps_realm.id,
    alias="ol-registration-flow",
    provider_id="basic-flow",
    opts=resource_options,
)
ol_registration_flow_page_form = keycloak.authentication.Execution(
    "ol-registration-flow-page-form",
    realm_id=ol_apps_realm.id,
    parent_flow_alias=ol_registration_flow.alias,
    authenticator="registration-page-form",
    requirement="REQUIRED",
    opts=resource_options,
)
ol_registration_form = keycloak.authentication.Subflow(
    "ol-registration-form",
    realm_id=ol_apps_realm.id,
    #authenticator="registration-page-form",
    alias="ol-registration-form",
    parent_flow_alias=ol_registration_flow.alias,
    provider_id="form-flow",
    requirement="REQUIRED",
    opts=resource_options,
)
ol_registration_flow_registration_user_creation_step = keycloak.authentication.Execution
(
    "ol-registration-flow-registration-user-creation-step",
    realm_id=ol_apps_realm.id,
    parent_flow_alias=ol_registration_form.alias,
    authenticator="registration-user-creation",
    requirement="REQUIRED",
    opts=resource_options,
)
ol_registration_flow_profile_validation_step = keycloak.authentication.Execution(
    "ol-registration-flow-profile-validation-step",
    realm_id=ol_apps_realm.id,
    parent_flow_alias=ol_registration_form.alias,
    authenticator="registration-profile-action",
    requirement="REQUIRED",
    opts=ResourceOptions(
        provider=keycloak_provider,
        depends_on=ol_registration_flow_registration_user_creation_step
    ),
)
ol_registration_flow_password_validation_step = keycloak.authentication.Execution(
    "ol-registration-flow-password-validation-step",
    realm_id=ol_apps_realm.id,
    parent_flow_alias=ol_registration_form.alias,
    authenticator="registration-password-action",
    requirement="REQUIRED",
    opts=ResourceOptions(
        provider=keycloak_provider,
        depends_on=ol_registration_flow_profile_validation_step
    ),
)
ol_registration_flow_recaptcha_step = keycloak.authentication.Execution(
    "ol-registration-flow-recaptcha-step",
    realm_id=ol_apps_realm.id,
    parent_flow_alias=ol_registration_form.alias,
    authenticator="registration-recaptcha-action",
    requirement="REQUIRED",
    opts=ResourceOptions(
        provider=keycloak_provider,
        depends_on=ol_registration_flow_password_validation_step
    ),
)
ol_registration_flow_recaptcha_step_config = keycloak.authentication.ExecutionConfig(
    "ol-registration-flow-recaptcha-step-config",
    realm_id=ol_apps_realm.id,
    execution_id=ol_registration_flow_recaptcha_step.id,
    alias="google-recaptcha-v2",
    config={
        "recaptchaSiteKey": keycloak_realm_config.get(captcha_site_key),
        "recaptchaSiteSecret": keycloak_realm_config.get(captcha_secret_key),
    },
    opts=resource_options,
)
ol_registration_flow_binding = keycloak.authentication.Bindings(
    "ol-registration-flow-binding",
    realm_id=ol_apps_realm.id,
    registration_flow=ol_registration_flow.alias,
    opts=resource_options,
)
# OL - Registration flow [END]
"""
# OL - browser flow [START]
# username-form -> ol-auth-username-password-form
ol_browser_flow = keycloak.authentication.Flow(
    "ol-browser",
    realm_id=ol_apps_realm.id,
    alias="ol-browser",
    opts=resource_options,
)
ol_browser_cookie = keycloak.authentication.Execution(
    "auth-cookie",
    realm_id=ol_apps_realm.id,
    parent_flow_alias=ol_browser_flow.alias,
    authenticator="auth-cookie",
    requirement="ALTERNATIVE",
    opts=resource_options,
)
ol_browser_flow_forms = keycloak.authentication.Subflow(
    "ol-browser-forms",
    realm_id=ol_apps_realm.id,
    alias="ol-browser forms",
    parent_flow_alias=ol_browser_flow.alias,
    provider_id="basic-flow",
    requirement="ALTERNATIVE",
    opts=ResourceOptions(
        provider=keycloak_provider,
        depends_on=ol_browser_cookie,
    ),
)
ol_browser_flow_username_form = keycloak.authentication.Execution(
    "auth-username-form",
    realm_id=ol_apps_realm.id,
    parent_flow_alias=ol_browser_flow_forms.alias,
    authenticator="auth-username-form",
    requirement="REQUIRED",
    opts=resource_options,
)
ol_browser_flow_ol_auth_username_password_form = keycloak.authentication.Execution(
    "auth-username-password-form",
    realm_id=ol_apps_realm.id,
    parent_flow_alias=ol_browser_flow_forms.alias,
    authenticator="auth-username-password-form",
    requirement="REQUIRED",
    opts=resource_options,
)
# Bind the flow to the olapps realm for browser login.
browser_authentication_binding = keycloak.authentication.Bindings(
    "browserAuthenticationBinding",
    realm_id=ol_apps_realm.id,
    browser_flow=ol_browser_flow.alias,
    opts=resource_options,
)
# OL - browser flow [END]

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
        attribute_name="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
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
        attribute_name="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname",
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
        attribute_name="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname",
        identity_provider_alias=ol_apps_touchstone_saml_identity_provider.alias,
        user_attribute="firstName",
        extra_config={
            "syncMode": "INHERIT",
        },
        opts=resource_options,
    ),
    keycloak.AttributeImporterIdentityProviderMapper(
        "map-touchstone-saml-full-name-attribute",
        realm=ol_data_platform_realm.id,
        attribute_name="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
        identity_provider_alias=ol_data_platform_touchstone_saml_identity_provider.alias,
        user_attribute="fullName",
        extra_config={
            "syncMode": "INHERIT",
        },
        opts=resource_options,
    ),
    keycloak.HardcodedAttributeIdentityProviderMapper(
        "map-touchstone-email-opt-in-attribute",
        name="email-opt-in-default",
        realm=ol_data_platform_realm.id,
        identity_provider_alias=ol_data_platform_touchstone_saml_identity_provider.alias,
        attribute_name="emailOptIn",
        attribute_value="true",
        extra_config={
            "syncMode": "INHERIT",
        },
    ),
)
# Touchstone SAML [END]

if stack_info.env_suffix in ["ci", "qa"]:
    # OL-DEV-FAKE-TOUCHSTONE [START] # noqa: ERA001
    ol_apps_dev_fake_touchstone_ci_identity_provider = keycloak.saml.IdentityProvider(
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
        signing_certificate=keycloak_realm_config.get("fake_touchstone_sig_cert"),
        want_assertions_encrypted=True,
        want_assertions_signed=True,
        opts=resource_options,
        first_broker_login_flow_alias=ol_touchstone_first_login_flow.alias,
        hide_on_login_page=False,
        gui_order="60",
    )
    oidc_attribute_importer_identity_provider_mapper = (
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
        ),
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
    # OKTA-DEV [END] # noqa: ERA001
