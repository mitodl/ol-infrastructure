import pulumi_keycloak as keycloak

from pulumi import Config
from ol_infrastructure.lib.pulumi_helper import parse_stack

env_config = Config("environment")
stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
keycloak_config = Config("keycloak")


# Create a Keycloak provider instance
provider = keycloak.Provider(
    "identity-provider",
    base_url=f"https://identity-{env_name}.odl.mit.edu/auth/",
    realm="master",
    client_id="",
    client_secret="",
)

# Create OL Platform Engineering Realm
realm = keycloak.Realm(
    "ol-platform-engineering",
    access_code_lifespan="1h",
    attributes={
        "mycustomAttribute": "myCustomValue",
    },
    display_name="OL PLatform Engineering",
    display_name_html="<b>OL PLatform Engineering</b>",
    enabled=True,
    login_theme="base",
    duplicate_emails_allowed=False,
    otp_policy=keycloak.RealmOptPolicyArgs(
        algorithm="HmacSHA256",
        digits=6,
        initial_counter=2,
        look_ahead_window=1,
        period=30,
        type="totp",
    ),
    reset_password_allowed=True,
    verify_email=True,
    password_policy="upperCase(2) and length(30) and forceExpiredPasswordChange(365) and notUsername",  # noqa: S106, E501
    realm="ol-platform-engineering",
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
            password=keycloak_config.email_password,
            username=keycloak_config.email_username,
        ),
        from_="odl-devops@mit.edu",
        from_display_name="Identity - OL PLatform Engineering",
        host=keycloak_config.email_host,
        port=keycloak_config.email_host,
        reply_to="odl-devops@mit.edu",
        reply_to_display_name="Identity - OL Platform Engineering",
        starttls=True,
    ),
    ssl_required="external",
)
