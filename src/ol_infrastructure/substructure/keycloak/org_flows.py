"""Keycloak authentication flows for organization-specific login processes."""

import pulumi
import pulumi_keycloak as keycloak


def create_organization_browser_flows(realm_id: str | pulumi.Output[str]):
    """Create Keycloak authentication flows for organization browser login.

    realm_id: The ID of the Keycloak realm where the flows will be created.
    """
    # Organization browser Browser - Conditional OTP
    org_browser_conditional_otp_flow = keycloak.AuthenticationFlow(
        "organization-browser-conditional-otp-flow",
        realm_id=realm_id,
        alias="Organization browser Browser - Conditional OTP",
        description="Flow to determine if the OTP is required for the authentication",
        provider_id="basic-flow",
        top_level=False,
        built_in=False,
    )

    keycloak.AuthenticationExecution(
        "org-browser-conditional-user-configured-exec",
        realm_id=realm_id,
        parent_flow_id=org_browser_conditional_otp_flow.id,
        authenticator="conditional-user-configured",
        requirement="REQUIRED",
        priority=10,
        authenticator_flow=False,
    )

    keycloak.AuthenticationExecution(
        "org-browser-auth-otp-form-exec",
        realm_id=realm_id,
        parent_flow_id=org_browser_conditional_otp_flow.id,
        authenticator="auth-otp-form",
        requirement="REQUIRED",
        priority=20,
        authenticator_flow=False,
    )

    # Organization browser forms
    org_browser_forms_flow = keycloak.AuthenticationFlow(
        "organization-browser-forms-flow",
        realm_id=realm_id,
        alias="Organization browser forms",
        description="Username, password, otp and other auth forms.",
        provider_id="basic-flow",
        top_level=False,
        built_in=False,
    )

    keycloak.AuthenticationExecution(
        "org-browser-forms-has-credential-exec",
        realm_id=realm_id,
        parent_flow_id=org_browser_forms_flow.id,
        authenticator="has-credential-authenticator",
        requirement="REQUIRED",
        priority=21,
        authenticator_flow=False,
    )

    keycloak.AuthenticationExecution(
        "org-browser-forms-password-form-exec",
        realm_id=realm_id,
        parent_flow_id=org_browser_forms_flow.id,
        authenticator="auth-password-form",
        requirement="REQUIRED",
        priority=22,
        authenticator_flow=False,
    )

    keycloak.AuthenticationFlowExecution(
        "org-browser-forms-conditional-otp-flow-exec",
        realm_id=realm_id,
        parent_flow_id=org_browser_forms_flow.id,
        flow_id=org_browser_conditional_otp_flow.id,
        requirement="CONDITIONAL",
        priority=23,
    )

    # Organization browser (Top-level)
    org_browser_flow = keycloak.AuthenticationFlow(
        "organization-browser-flow",
        realm_id=realm_id,
        alias="Organization browser",
        description="browser based authentication with organization redirect",
        provider_id="basic-flow",
        top_level=True,
        built_in=False,
    )

    keycloak.AuthenticationExecution(
        "org-browser-cookie-exec",
        realm_id=realm_id,
        parent_flow_id=org_browser_flow.id,
        authenticator="auth-cookie",
        requirement="ALTERNATIVE",
        priority=10,
        authenticator_flow=False,
    )

    keycloak.AuthenticationExecution(
        "org-browser-spnego-exec",
        realm_id=realm_id,
        parent_flow_id=org_browser_flow.id,
        authenticator="auth-spnego",
        requirement="DISABLED",
        priority=20,
        authenticator_flow=False,
    )

    keycloak.AuthenticationExecution(
        "org-browser-idp-redirector-exec",
        realm_id=realm_id,
        parent_flow_id=org_browser_flow.id,
        authenticator="identity-provider-redirector",
        requirement="ALTERNATIVE",
        priority=25,
        authenticator_flow=False,
    )

    keycloak.AuthenticationExecution(
        "org-browser-organization-exec",
        realm_id=realm_id,
        parent_flow_id=org_browser_flow.id,
        authenticator="organization",
        requirement="ALTERNATIVE",
        priority=30,
        authenticator_flow=False,
    )

    keycloak.AuthenticationFlowExecution(
        "org-browser-forms-flow-exec",
        realm_id=realm_id,
        parent_flow_id=org_browser_flow.id,
        flow_id=org_browser_forms_flow.id,
        requirement="ALTERNATIVE",
        priority=32,
    )

    return org_browser_flow


def create_organization_first_broker_login_flows(realm_id: str | pulumi.Output[str]):
    """Create Keycloak authentication flows for organization first broker login.

    realm_id: The ID of the Keycloak realm where the flows will be created.
    """
    # Organization first broker login First broker login - Conditional OTP
    org_first_broker_conditional_otp_flow = keycloak.AuthenticationFlow(
        "organization-first-broker-conditional-otp-flow",
        realm_id=realm_id,
        alias="Organization first broker login First broker login - Conditional OTP",
        description="Flow to determine if the OTP is required for the authentication",
        provider_id="basic-flow",
        top_level=False,
        built_in=False,
    )

    keycloak.AuthenticationExecution(
        "org-first-broker-conditional-user-configured-exec",
        realm_id=realm_id,
        parent_flow_id=org_first_broker_conditional_otp_flow.id,
        authenticator="conditional-user-configured",
        requirement="REQUIRED",
        priority=10,
        authenticator_flow=False,
    )

    keycloak.AuthenticationExecution(
        "org-first-broker-auth-otp-form-exec",
        realm_id=realm_id,
        parent_flow_id=org_first_broker_conditional_otp_flow.id,
        authenticator="auth-otp-form",
        requirement="REQUIRED",
        priority=20,
        authenticator_flow=False,
    )

    # Organization first broker login Verify Existing Account by Re-authentication
    org_first_broker_verify_existing_account_flow = keycloak.AuthenticationFlow(
        "organization-first-broker-verify-existing-account-flow",
        realm_id=realm_id,
        alias=(
            "Organization first broker login Verify Existing Account by"
            " Re-authentication"
        ),
        description="Reauthentication of existing account",
        provider_id="basic-flow",
        top_level=False,
        built_in=False,
    )

    keycloak.AuthenticationExecution(
        "org-first-broker-idp-username-password-form-exec",
        realm_id=realm_id,
        parent_flow_id=org_first_broker_verify_existing_account_flow.id,
        authenticator="idp-username-password-form",
        requirement="REQUIRED",
        priority=10,
        authenticator_flow=False,
    )

    keycloak.AuthenticationFlowExecution(
        "org-first-broker-verify-existing-account-conditional-otp-flow-exec",
        realm_id=realm_id,
        parent_flow_id=org_first_broker_verify_existing_account_flow.id,
        flow_id=org_first_broker_conditional_otp_flow.id,
        requirement="CONDITIONAL",
        priority=20,
    )

    # Organization first broker login Account verification options
    org_first_broker_account_verification_options_flow = keycloak.AuthenticationFlow(
        "organization-first-broker-account-verification-options-flow",
        realm_id=realm_id,
        alias="Organization first broker login Account verification options",
        description="Method with which to verify the existing account",
        provider_id="basic-flow",
        top_level=False,
        built_in=False,
    )

    keycloak.AuthenticationExecution(
        "org-first-broker-idp-email-verification-exec",
        realm_id=realm_id,
        parent_flow_id=org_first_broker_account_verification_options_flow.id,
        authenticator="idp-email-verification",
        requirement="ALTERNATIVE",
        priority=10,
        authenticator_flow=False,
    )

    keycloak.AuthenticationFlowExecution(
        "org-first-broker-account-verification-options-verify-existing-account-flow-exec",
        realm_id=realm_id,
        parent_flow_id=org_first_broker_account_verification_options_flow.id,
        flow_id=org_first_broker_verify_existing_account_flow.id,
        requirement="ALTERNATIVE",
        priority=20,
    )

    # Organization first broker login Handle Existing Account
    org_first_broker_handle_existing_account_flow = keycloak.AuthenticationFlow(
        "organization-first-broker-handle-existing-account-flow",
        realm_id=realm_id,
        alias="Organization first broker login Handle Existing Account",
        description=(
            "Handle what to do if there is existing account with same email/username"
            " like authenticated identity provider"
        ),
        provider_id="basic-flow",
        top_level=False,
        built_in=False,
    )

    keycloak.AuthenticationExecution(
        "org-first-broker-idp-confirm-link-exec",
        realm_id=realm_id,
        parent_flow_id=org_first_broker_handle_existing_account_flow.id,
        authenticator="idp-confirm-link",
        requirement="REQUIRED",
        priority=10,
        authenticator_flow=False,
    )

    keycloak.AuthenticationFlowExecution(
        "org-first-broker-handle-existing-account-account-verification-options-flow-exec",
        realm_id=realm_id,
        parent_flow_id=org_first_broker_handle_existing_account_flow.id,
        flow_id=org_first_broker_account_verification_options_flow.id,
        requirement="REQUIRED",
        priority=20,
    )

    # Organization first broker login User creation or linking
    org_first_broker_user_creation_or_linking_flow = keycloak.AuthenticationFlow(
        "organization-first-broker-user-creation-or-linking-flow",
        realm_id=realm_id,
        alias="Organization first broker login User creation or linking",
        description="Flow for the existing/non-existing user alternatives",
        provider_id="basic-flow",
        top_level=False,
        built_in=False,
    )

    org_first_broker_create_user_if_unique_exec = keycloak.AuthenticationExecution(
        "org-first-broker-create-user-if-unique-exec",
        realm_id=realm_id,
        parent_flow_id=org_first_broker_user_creation_or_linking_flow.id,
        authenticator="idp-create-user-if-unique",
        requirement="ALTERNATIVE",
        priority=10,
        authenticator_flow=False,
    )

    keycloak.AuthenticationExecutionConfig(
        "org-first-broker-create-unique-user-config",
        realm_id=realm_id,
        execution_id=org_first_broker_create_user_if_unique_exec.id,
        alias="Organization first broker login create unique user config",
        config={},  # Placeholder: Actual config properties are not provided in the JSON
    )

    keycloak.AuthenticationFlowExecution(
        "org-first-broker-user-creation-or-linking-handle-existing-account-flow-exec",
        realm_id=realm_id,
        parent_flow_id=org_first_broker_user_creation_or_linking_flow.id,
        flow_id=org_first_broker_handle_existing_account_flow.id,
        requirement="ALTERNATIVE",
        priority=21,
    )

    # Organization first broker login (Top-level)
    org_first_broker_login_flow = keycloak.AuthenticationFlow(
        "organization-first-broker-login-flow",
        realm_id=realm_id,
        alias="Organization first broker login",
        description=(
            "Actions taken after first broker login with identity provider account,"
            " which is not yet linked to any Keycloak account, accounting for"
            " organization flow"
        ),
        provider_id="basic-flow",
        top_level=True,
        built_in=False,
    )

    org_first_broker_idp_review_profile_exec = keycloak.AuthenticationExecution(
        "org-first-broker-idp-review-profile-exec",
        realm_id=realm_id,
        parent_flow_id=org_first_broker_login_flow.id,
        authenticator="idp-review-profile",
        requirement="REQUIRED",
        priority=10,
        authenticator_flow=False,
    )

    keycloak.AuthenticationExecutionConfig(
        "org-first-broker-login-review-profile-config",
        realm_id=realm_id,
        execution_id=org_first_broker_idp_review_profile_exec.id,
        alias="Organization first broker login review profile config",
        config={},  # Placeholder: Actual config properties are not provided in the JSON
    )

    keycloak.AuthenticationFlowExecution(
        "org-first-broker-login-user-creation-or-linking-flow-exec",
        realm_id=realm_id,
        parent_flow_id=org_first_broker_login_flow.id,
        flow_id=org_first_broker_user_creation_or_linking_flow.id,
        requirement="REQUIRED",
        priority=22,
    )

    keycloak.AuthenticationExecution(
        "org-first-broker-idp-add-organization-member-exec",
        realm_id=realm_id,
        parent_flow_id=org_first_broker_login_flow.id,
        authenticator="idp-add-organization-member",
        requirement="REQUIRED",
        priority=23,
        authenticator_flow=False,
    )

    return org_first_broker_login_flow
