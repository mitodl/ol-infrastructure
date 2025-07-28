"""Keycloak substructure definition."""

import json
import urllib.request
from functools import partial

import pulumi_keycloak as keycloak
import pulumi_vault as vault
from pulumi import Config, Output, ResourceOptions

from bridge.lib.magic_numbers import SECONDS_IN_ONE_DAY
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider
from ol_infrastructure.substructure.keycloak.org_flows import (
    create_organization_browser_flows,
    create_organization_first_broker_login_flows,
    create_organization_scope,
)

env_config = Config("environment")
stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
keycloak_config = Config("keycloak")
keycloak_realm_config = Config("keycloak_realm")
setup_vault_provider()


def fetch_realm_public_key(keycloak_url: str, realm_id: str) -> str:
    """Fetch the public key for a given Keycloak realm."""
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
session_secret = keycloak_realm_config.require("session_secret")
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
    email_theme="ol-learn",
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
        starttls=False,
    ),
    ssl_required="external",
    offline_session_idle_timeout="168h",
    organizations_enabled=True,
    sso_session_idle_timeout="336h",
    sso_session_max_lifespan="336h",
    opts=resource_options,
)

olapps_realm_events = keycloak.RealmEvents(
    "realmEvents",
    realm_id=ol_apps_realm.realm,
    events_enabled=True,
    events_expiration=SECONDS_IN_ONE_DAY,
    admin_events_enabled=True,
    admin_events_details_enabled=True,
    events_listeners=["jboss-logging"],
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

ol_apps_required_action_update_email = keycloak.RequiredAction(
    "ol-apps-update-email",
    realm_id=ol_apps_realm.realm,
    alias="UPDATE_EMAIL",
    default_action=False,
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

ol_apps_user_email_optin_attribute_mapper = keycloak.openid.UserAttributeProtocolMapper(
    "email-optin-mapper",
    realm_id=ol_apps_realm.id,
    client_scope_id=ol_apps_profile_client_scope.id,
    name="email-optin-mapper",
    user_attribute="emailOptIn",
    claim_name="email_optin",
)
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

ol_data_platforme_realm_events = keycloak.RealmEvents(
    "ol-data-platforme-realm-events",
    realm_id=ol_data_platform_realm.realm,
    events_enabled=True,
    events_expiration=604800,
    admin_events_enabled=True,
    admin_events_details_enabled=True,
    enabled_event_types=[],
    events_listeners=["jboss-logging"],
)

ol_data_required_action_verify_email = keycloak.RequiredAction(
    "ol-data-verify-email",
    realm_id=ol_platform_engineering_realm.realm,
    alias="VERIFY_EMAIL",
    default_action=False,
    enabled=False,
    opts=resource_options,
)

fetch_realm_public_key_partial = partial(
    fetch_realm_public_key,
    keycloak_url,
)

# OpenID Clients [START]
# OLAPPS REALM - OpenID Clients [START]

# Unified Ecommerce Client [START]
olapps_unified_ecommerce_client = keycloak.openid.Client(
    "olapps-unified-ecommerce-client",
    name="ol-unified-ecommerce-client",
    realm_id="olapps",
    client_id="ol-unified-ecommerce-client",
    client_secret=keycloak_realm_config.get("olapps-unified-ecommerce-client-secret"),
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
olapps_unified_ecommerce_client_scope = keycloak.openid.ClientDefaultScopes(
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
olapps_unified_ecommerce_client_roles = keycloak_realm_config.get_object(
    "olapps-unified-ecommerce-client-roles"
)
for role in olapps_unified_ecommerce_client_roles:
    keycloak.Role(
        f"olapps-unified-ecommerce-client-{role}",
        name=role,
        realm_id="olapps",
        client_id=olapps_unified_ecommerce_client.id,
        opts=resource_options,
    )
olapps_unified_ecommerce_client_data = vault.generic.Secret(
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
olapps_learn_ai_client_scope = keycloak.openid.ClientDefaultScopes(
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
olapps_learn_ai_client_roles = keycloak_realm_config.get_object(
    "olapps-learn-ai-client-roles"
)
for role in olapps_learn_ai_client_roles:
    keycloak.Role(
        f"olapps-learn-ai-client-{role}",
        name=role,
        realm_id="olapps",
        client_id=olapps_learn_ai_client.id,
        opts=resource_options,
    )
olapps_learn_ai_client_data = vault.generic.Secret(
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
    olapps_mitlearn_client_scope = keycloak.openid.ClientDefaultScopes(
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
    olapps_mitlearn_client_data = vault.generic.Secret(
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
    client_secret=keycloak_realm_config.get("olapps-open-discussions-client-secret"),
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
olapps_open_discussions_client_data = vault.generic.Secret(
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
olapps_mitxonline_client_data = vault.generic.Secret(
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

# OLAPPS REALM - OpenID Clients [START]

# OL-PLATFORM-ENGINEERING REALM - OpenID Clients [START]

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
ol_platform_engineering_airbyte_client_data = vault.generic.Secret(
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
ol_platform_engineering_dagster_client_data = vault.generic.Secret(
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
ol_platform_engineering_leek_client_data = vault.generic.Secret(
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
    ol_platform_engineering_vault_client_data = vault.generic.Secret(
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
# OL-PLATFORM-ENGINEERING REALM - OpenID Clients [END]

# OL-DATA-PLATFORM REALM - OpenID Clients [START]
# SUPERSET [START] # noqa: ERA001
ol_data_platform_superset_client = keycloak.openid.Client(
    "ol-data-platform-superset-client",
    name="ol-data-platform-superset-client",
    realm_id="ol-data-platform",
    client_id="ol-superset-client",
    client_secret=keycloak_realm_config.get("ol-data-platform-superset-client-secret"),
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
ol_data_platform_superset_client_roles = keycloak_realm_config.get_object(
    "ol-data-platform-superset-client-roles"
)
ol_data_platform_superset_client_role_refs = {}
for role in ol_data_platform_superset_client_roles:
    role_ref = keycloak.Role(
        f"ol-data-platform-superset-client-{role}",
        name=role,
        realm_id="ol-data-platform",
        client_id=ol_data_platform_superset_client.id,
        opts=resource_options,
    )
    ol_data_platform_superset_client_role_refs[role] = role_ref

ol_data_platform_superset_client_data = vault.generic.Secret(
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
            lambda realm_id: fetch_realm_public_key_partial(realm_id)
        ),
    ).apply(json.dumps),
)

# Create realm roles for ol-data-platform
ol_data_platform_eng_data_role = keycloak.Role(
    "ol-data-platform-eng-data-role",
    realm_id=ol_data_platform_realm.id,
    name="ol-eng-data",
    description="OL Engineering Data role - maps to superset_admin",
    composite_roles=[ol_data_platform_superset_client_role_refs["superset_admin"].id],
    opts=resource_options,
)

ol_data_platform_eng_developer_role = keycloak.Role(
    "ol-data-platform-eng-developer-role",
    realm_id=ol_data_platform_realm.id,
    name="ol-eng-developer",
    description="OL Engineering Developer role - maps to superset_alpha",
    composite_roles=[ol_data_platform_superset_client_role_refs["superset_alpha"].id],
    opts=resource_options,
)

ol_data_platform_eng_reporter_role = keycloak.Role(
    "ol-data-platform-eng-reporter-role",
    realm_id=ol_data_platform_realm.id,
    name="ol-eng-reporter",
    description="OL Engineering Reporter role - maps to superset_gamma",
    composite_roles=[ol_data_platform_superset_client_role_refs["superset_gamma"].id],
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

ol_data_platform_role_keys_openid_client_scope_mapper = (
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
    )
)

ol_data_platform_superset_client_scope = keycloak.openid.ClientDefaultScopes(
    "ol-data-platform-superset-client-default-scopes",
    realm_id="ol-data-platform",
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
ol_data_platform_openmetadata_client_roles = keycloak_realm_config.get_object(
    "ol-data-platform-openmetadata-client-roles"
)
for role in ol_data_platform_openmetadata_client_roles:
    keycloak.Role(
        f"ol-data-platform-openmetadata-client-{role}",
        name=role,
        realm_id="ol-data-platform",
        client_id=ol_data_platform_openmetadata_client.id,
        opts=resource_options,
    )
ol_data_platform_openmetadata_client_data = vault.generic.Secret(
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
            lambda realm_id: fetch_realm_public_key_partial(realm_id)
        ),
    ).apply(json.dumps),
)
# OPENMETADATA [END] # noqa: ERA001

# OL-DATA-PLATFORM REALM - OpenID Clients [END]
# OpenID Clients [END]

# OL Data Platform Realm - Authentication Flows[START]
# OL - browser flow [START]
# username-form -> ol-auth-username-password-form

ol_browser_data_platform_flow = keycloak.authentication.Flow(
    "ol-browser-data-platform-flow",
    realm_id=ol_data_platform_realm.id,
    alias="ol-browser-data-platform-flow",
    opts=resource_options,
)
ol_browser_data_platform_cookie = keycloak.authentication.Execution(
    "ol-browser-data-platform-auth-cookie",
    realm_id=ol_data_platform_realm.id,
    parent_flow_alias=ol_browser_data_platform_flow.alias,
    authenticator="auth-cookie",
    requirement="ALTERNATIVE",
    priority=10,
    opts=resource_options,
)
ol_browser_data_platform_idp_redirector = keycloak.authentication.Execution(
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
ol_browser_data_platform_flow_org_user_configured = keycloak.authentication.Execution(
    "ol-browser-data-platform_flow-org-user-configured",
    realm_id=ol_data_platform_realm.id,
    parent_flow_alias=ol_browser_data_platform_flow_org.alias,
    authenticator="conditional-user-configured",
    priority=40,
    requirement="REQUIRED",
    opts=resource_options,
)
ol_browser_data_platform_flow_org_identity_first = keycloak.authentication.Execution(
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
ol_browser_data_platform_flow_username_form = keycloak.authentication.Execution(
    "ol-browser-data-platform-flow-username-form",
    realm_id=ol_data_platform_realm.id,
    parent_flow_alias=ol_browser_data_platform_passkey_flow.alias,
    authenticator="auth-username-form",
    requirement="REQUIRED",
    priority=70,
    opts=resource_options,
)
ol_browser_data_platform_webauthn_authenticator_flow = (
    keycloak.authentication.Execution(
        "ol-browser-data-platform-webauthn-authenticator-flow",
        realm_id=ol_data_platform_realm.id,
        parent_flow_alias=ol_browser_data_platform_passkey_flow.alias,
        authenticator="webauthn-authenticator-passwordless",
        requirement="REQUIRED",
        priority=80,
        opts=resource_options,
    )
)
# Bind the flow to the ol-data-platform realm for browser login.
ol_data_platform_browser_authentication_binding = keycloak.authentication.Bindings(
    "ol-data-platform-browser-authentication-binding",
    realm_id=ol_data_platform_realm.id,
    browser_flow=ol_browser_data_platform_flow.alias,
    opts=resource_options,
)
# OL Data Platform - browser flow [END]
# First login flow [START]
# Does not require email verification or confirmation to connect with existing account.
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
ol_touchstone_first_login_review_profile_config = (
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
)
ol_data_platform_touchstone_user_creation_or_linking_subflow = (
    keycloak.authentication.Subflow(
        "ol-data-platform-touchstone-user-creation-or-linking-subflow",
        realm_id=ol_data_platform_realm.id,
        alias="ol-data-platform-touchstone-first-broker-login-user-creation-or-linking",
        parent_flow_alias=ol_data_platform_touchstone_first_login_flow.alias,
        provider_id="basic-flow",
        priority=20,
        requirement="REQUIRED",
        opts=resource_options,
    )
)
ol_data_platform_touchstone_user_creation_or_linking_subflow_create_user_if_unique_step = keycloak.authentication.Execution(  # noqa: E501
    "ol-data-platform-touchstone-create-user-if-unique",
    realm_id=ol_data_platform_realm.id,
    parent_flow_alias=ol_data_platform_touchstone_user_creation_or_linking_subflow.alias,
    authenticator="idp-create-user-if-unique",
    priority=30,
    requirement="ALTERNATIVE",
    opts=resource_options,
)
ol_data_platform_touchstone_user_creation_or_linking_subflow_automatically_set_existing_user_step = keycloak.authentication.Execution(  # noqa: E501
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
ol_data_platform_oidc_attribute_importer_identity_provider_mapper = (
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
    ),
)
# OL Data Platform - Touchstone SAML [END]

# OLAPPS REALM- First login flow [START]
# Does not require email verification or confirmation to connect with existing account.
ol_first_login_flow = create_organization_first_broker_login_flows(
    ol_apps_realm.id, "olapps", opts=resource_options
)
# OL - First login flow [END]

"""
# This can be uncommented once this issue is resolved - https://github.com/pulumi/pulumi-keycloak/issues/655
# noqa: ERA001,E501
# OL - Registration flow [START]
ol_registration_flow = keycloak.authentication.Flow(
    "ol-registration-flow",
    realm_id=ol_apps_realm.id,
    alias="ol-registration-flow",
    provider_id="basic-flow",
    opts=resource_options,
)
ol_registration_form = keycloak.authentication.Subflow(
    "ol-registration-form",
    realm_id=ol_apps_realm.id,
    authenticator="registration-page-form",
    alias="ol-registration-form",
    parent_flow_alias=ol_registration_flow.alias,
    provider_id="form-flow",
    requirement="REQUIRED",
    opts=resource_options,
)
ol_registration_flow_registration_user_creation_step = (
    keycloak.authentication.Execution(
        "ol-registration-flow-registration-user-creation-step",
        realm_id=ol_apps_realm.id,
        parent_flow_alias=ol_registration_form.alias,
        authenticator="registration-user-creation",
        requirement="REQUIRED",
        opts=resource_options,
    )
)
ol_registration_flow_password_validation_step = keycloak.authentication.Execution(
    "ol-registration-flow-password-validation-step",
    realm_id=ol_apps_realm.id,
    parent_flow_alias=ol_registration_form.alias,
    authenticator="registration-password-action",
    requirement="REQUIRED",
    opts=ResourceOptions(
        provider=keycloak_provider,
        depends_on=ol_registration_flow_registration_user_creation_step
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
ol_browser_flow = create_organization_browser_flows(
    ol_apps_realm.id, "olapps", opts=resource_options
)
# Bind the flow to the olapps realm for browser login.
ol_apps_authentication_flow_binding = keycloak.authentication.Bindings(
    "ol-apps-flow-bindings",
    realm_id=ol_apps_realm.id,
    browser_flow=ol_browser_flow.alias,
    first_broker_login_flow=ol_first_login_flow.alias,
    opts=resource_options,
)
# OL - browser flow [END]
# Ensure organization scope is present
ol_apps_org_scope = create_organization_scope(
    ol_apps_realm.id, "olapps", resource_options
)

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
    first_broker_login_flow_alias=ol_first_login_flow.alias,
)
oidc_attribute_importer_identity_provider_mapper = (
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
    ),
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
    ),
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
    ),
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
    ),
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
        first_broker_login_flow_alias=ol_first_login_flow.alias,
        hide_on_login_page=False,
        gui_order="60",
    )
    oidc_attribute_importer_identity_provider_mapper = (  # type: ignore[assignment]
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
        first_broker_login_flow_alias=ol_first_login_flow.alias,
        opts=resource_options,
    )
    oidc_attribute_importer_identity_provider_mapper = (  # type: ignore[assignment]
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
