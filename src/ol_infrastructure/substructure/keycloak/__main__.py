import json
import pulumi_keycloak as keycloak

from pulumi import Config, ResourceOptions
from ol_infrastructure.lib.pulumi_helper import parse_stack

env_config = Config("environment")
stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
keycloak_config = Config("keycloak")
keycloak_clients = keycloak_config.get("clients")


# Create a Keycloak provider cause we ran into an issue with pulumi reading
# config from stack definition.
keycloak_provider = keycloak.Provider(
    "keycloak_provider",
    url=keycloak_config.get("url"),
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
    password_policy="upperCase(2) and digits(4) and length(30) and specialChars(4) and forceExpiredPasswordChange(365) and notUsername and notEmail",  # noqa: E501,S106 # pragma: allowlist secret
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
            content_security_policy="frame-src 'self'; frame-ancestors 'self'; object-src 'none';",  # noqa: E501
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

# Check if any OIDC clients exist in config and create them
if keycloak_clients:
    keycloak_clients = json.loads(keycloak_clients)
    for client_name, client_domain in keycloak_clients.items():
        openid_client = keycloak.openid.Client(
            f"ol-{client_name}-client",
            realm_id=ol_platform_engineering_realm.realm,
            client_id=f"ol-{client_name}-client",
            enabled=True,
            access_type="CONFIDENTIAL",
            standard_flow_enabled=True,
            valid_redirect_uris=[f"{client_domain}/*"],
            login_theme="keycloak",
            opts=resource_options,
        )

    # # Create Airbyte OIDC client
    # airbyte_domain_name = keycloak_clients["airbyte"]
    # airbyte_openid_client = keycloak.openid.Client(
    #     "ol-airbyte-client",
    #     realm_id=ol_platform_engineering_realm.realm,
    #     client_id="ol-airbyte-client",
    #     enabled=True,
    #     access_type="CONFIDENTIAL",
    #     standard_flow_enabled=True,
    #     valid_redirect_uris=[f"{airbyte_domain_name}/*"],
    #     login_theme="keycloak",
    #     opts=resource_options,
    # )

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
    reset_password_allowed=True,
    verify_email=True,
    password_policy="upperCase(1) and digits(1) and specialChars(1) and length(8) and notUsername and notEmail",  # noqa: E501,S106 # pragma: allowlist secret
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
            content_security_policy="frame-src 'self'; frame-ancestors 'self'; object-src 'none';",  # noqa: E501
            content_security_policy_report_only="",
            strict_transport_security="max-age=31536000; includeSubDomains",
            x_content_type_options="nosniff",
            x_frame_options="https://www.google.com",  # CAPTCHA
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
    default_action=True,
    enabled=True,
    opts=resource_options,
)

ol_apps_touchstone_saml_identity_provider = keycloak.saml.IdentityProvider(
    "touchstone-idp",
    realm=ol_apps_realm.id,
    alias="touchstone-idp",
    display_name="MIT Touchstone",
    entity_id=f"{keycloak_url}/realms/olapps",
    force_authn=True,
    post_binding_response=True,
    principal_attribute="",
    signing_certificate="",
    single_sign_on_service_url="https://idp.mit.edu/idp/Authn/MIT",
    store_token=False,
    trust_email=True,
    validate_signature=True,
    want_assertions_encrypted=True,
    want_assertions_signed=True,
    opts=resource_options,
)
