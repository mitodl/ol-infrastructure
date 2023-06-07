import pulumi_keycloak as keycloak

from pulumi import Config, ResourceOptions
from ol_infrastructure.lib.pulumi_helper import parse_stack

env_config = Config("environment")
stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
keycloak_config = Config("keycloak")


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
email_host = keycloak_config.require("email_host")
email_password = keycloak_config.require("email_password")
email_username = keycloak_config.require("email_username")

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
    login_theme="base",
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
    password_policy="upperCase(2) and digits(4) and length(30) and specialChars(4) and forceExpiredPasswordChange(365) and notUsername and notEmail",  # noqa: S106, E501 # pragma: allowlist secret
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

# Create Dagster OIDC client
dagster_domain_name = keycloak_config.require("url")
dagster_openid_client = keycloak.openid.Client(
    "ol-dagster-client",
    realm_id=ol_platform_engineering_realm.realm,
    client_id="ol-dagster-client",
    enabled=True,
    access_type="CONFIDENTIAL",
    standard_flow_enabled=True,
    valid_redirect_uris=[f"{dagster_domain_name}/*"],
    login_theme="keycloak",
    opts=resource_options,
)
