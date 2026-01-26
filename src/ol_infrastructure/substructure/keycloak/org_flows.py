"""Keycloak authentication flows for organization-specific login processes."""

import pulumi
import pulumi_keycloak as keycloak


def create_organization_scope(
    realm_id: str | pulumi.Output[str],
    realm_name: str,
    opts: pulumi.ResourceOptions | None = None,
) -> keycloak.openid.ClientScope:
    """Create a client scope for organization claims."""
    single_organization_scope = keycloak.openid.ClientScope(
        f"{realm_name}_single_organization_scope",
        realm_id=realm_id,
        name="organization",
        description="Additional claims about the organization a subject belongs to",
        include_in_token_scope=True,
        consent_screen_text="${organizationScopeConsentText}",
        opts=opts,
    )
    keycloak.GenericProtocolMapper(
        f"{realm_name}_single_organization_scope_mapper",
        realm_id=realm_id,
        client_scope_id=single_organization_scope.id,
        name="organization",
        protocol="openid-connect",
        protocol_mapper="oidc-organization-membership-mapper",
        config={
            "access.token.claim": "true",
            "addOrganizationAttributes": "true",
            "addOrganizationId": "true",
            "claim.name": "organization",
            "id.token.claim": "true",
            "introspection.token.claim": "true",
            "jsonType.label": "JSON",
            "lightweight.claim": "false",
            "multivalued": "true",
            "userinfo.token.claim": "true",
        },
        opts=opts,
    )
    return single_organization_scope


def create_organization_browser_flows(
    realm_id: str | pulumi.Output[str],
    realm_name: str,
    opts: pulumi.ResourceOptions | None = None,
) -> keycloak.authentication.Flow:
    """Create the authentication flows for an organization-based browser login."""
    organization_browser_flow = keycloak.authentication.Flow(
        f"{realm_name}_organization_browser_flow",
        alias="Organization browser",
        realm_id=realm_id,
        provider_id="basic-flow",
        description="browser based authentication with organization redirect",
        opts=opts,
    )
    keycloak.authentication.Execution(
        f"{realm_name}_organization_browser_flow_cookie_execution",
        realm_id=realm_id,
        parent_flow_alias=organization_browser_flow.alias,
        authenticator="auth-cookie",
        requirement="ALTERNATIVE",
        priority=10,
        opts=opts,
    )
    keycloak.authentication.Execution(
        f"{realm_name}_organization_browser_flow_spnego_execution",
        realm_id=realm_id,
        parent_flow_alias=organization_browser_flow.alias,
        authenticator="auth-spnego",
        requirement="DISABLED",
        priority=20,
        opts=opts,
    )
    keycloak.authentication.Execution(
        f"{realm_name}_organization_browser_flow_idp_redirector_execution",
        realm_id=realm_id,
        parent_flow_alias=organization_browser_flow.alias,
        authenticator="identity-provider-redirector",
        requirement="ALTERNATIVE",
        priority=25,
        opts=opts,
    )
    keycloak.authentication.Execution(
        f"{realm_name}_organization_browser_flow_organization_execution",
        realm_id=realm_id,
        parent_flow_alias=organization_browser_flow.alias,
        authenticator="organization",
        requirement="ALTERNATIVE",
        priority=30,
        opts=opts,
    )
    organization_browser_forms_subflow = keycloak.authentication.Subflow(
        f"{realm_name}_organization_browser_forms_subflow",
        alias="Organization browser forms",
        realm_id=realm_id,
        parent_flow_alias=organization_browser_flow.alias,
        provider_id="basic-flow",
        requirement="ALTERNATIVE",
        priority=32,
        opts=opts,
    )
    keycloak.authentication.Execution(
        f"{realm_name}_organization_browser_forms_has_credential_execution",
        realm_id=realm_id,
        parent_flow_alias=organization_browser_forms_subflow.alias,
        authenticator="has-credential-authenticator",
        requirement="REQUIRED",
        priority=21,
        opts=opts,
    )
    keycloak.authentication.Execution(
        f"{realm_name}_organization_browser_forms_username_password_form_execution",
        realm_id=realm_id,
        parent_flow_alias=organization_browser_forms_subflow.alias,
        authenticator="auth-username-password-form",
        requirement="REQUIRED",
        priority=22,
        opts=opts,
    )
    organization_browser_conditional_otp_subflow = keycloak.authentication.Subflow(
        f"{realm_name}_organization_browser_conditional_otp_subflow",
        alias="Organization browser Browser - Conditional OTP",
        realm_id=realm_id,
        parent_flow_alias=organization_browser_forms_subflow.alias,
        provider_id="basic-flow",
        requirement="CONDITIONAL",
        priority=23,
        opts=opts,
    )
    keycloak.authentication.Execution(
        f"{realm_name}_organization_browser_conditional_otp_user_configured_execution",
        realm_id=realm_id,
        parent_flow_alias=organization_browser_conditional_otp_subflow.alias,
        authenticator="conditional-user-configured",
        requirement="REQUIRED",
        priority=10,
        opts=opts,
    )
    keycloak.authentication.Execution(
        f"{realm_name}_organization_browser_conditional_otp_form_execution",
        realm_id=realm_id,
        parent_flow_alias=organization_browser_conditional_otp_subflow.alias,
        authenticator="auth-otp-form",
        requirement="REQUIRED",
        priority=20,
        opts=opts,
    )
    return organization_browser_flow


def create_organization_first_broker_login_flows(
    realm_id: pulumi.Output[str],
    realm_name: str,
    opts: pulumi.ResourceOptions | None = None,
) -> keycloak.authentication.Flow:
    """Create authentication flows for the first broker login with an organization."""
    main_flow = keycloak.authentication.Flow(
        f"{realm_name}_organization_first_broker_login_flow",
        alias="Organization first broker login",
        realm_id=realm_id,
        provider_id="basic-flow",
        description=(
            "Actions taken after first broker login with identity provider account, "
            "which is not yet linked to any Keycloak account, accounting for "
            "organization flow"
        ),
        opts=opts,
    )
    review_profile_execution = keycloak.authentication.Execution(
        f"{realm_name}_org_first_broker_login_review_profile_execution",
        realm_id=realm_id,
        parent_flow_alias=main_flow.alias,
        authenticator="idp-review-profile",
        requirement="REQUIRED",
        priority=10,
        opts=opts,
    )
    keycloak.authentication.ExecutionConfig(
        f"{realm_name}_org_first_broker_login_review_profile_config",
        realm_id=realm_id,
        execution_id=review_profile_execution.id,
        alias="Organization first broker login review profile config",
        config={"updateProfileOnFirstLogin": "missing"},
        opts=opts,
    )
    user_creation_or_linking_subflow = keycloak.authentication.Subflow(
        f"{realm_name}_org_first_broker_login_user_creation_or_linking_subflow",
        alias="Organization first broker login User creation or linking",
        realm_id=realm_id,
        parent_flow_alias=main_flow.alias,
        provider_id="basic-flow",
        requirement="REQUIRED",
        priority=22,
        opts=opts,
    )
    create_user_if_unique_execution = keycloak.authentication.Execution(
        f"{realm_name}_org_first_broker_login_create_user_if_unique_execution",
        realm_id=realm_id,
        parent_flow_alias=user_creation_or_linking_subflow.alias,
        authenticator="idp-create-user-if-unique",
        requirement="ALTERNATIVE",
        priority=10,
        opts=opts,
    )
    keycloak.authentication.ExecutionConfig(
        f"{realm_name}_org_first_broker_login_create_user_if_unique_config",
        realm_id=realm_id,
        execution_id=create_user_if_unique_execution.id,
        alias="Organization first broker login create unique user config",
        config={},
        opts=opts,
    )
    handle_existing_account_subflow = keycloak.authentication.Subflow(
        f"{realm_name}_org_first_broker_login_handle_existing_account_subflow",
        alias="Organization first broker login Handle Existing Account",
        realm_id=realm_id,
        parent_flow_alias=user_creation_or_linking_subflow.alias,
        provider_id="basic-flow",
        requirement="ALTERNATIVE",
        priority=21,
        opts=opts,
    )
    keycloak.authentication.Execution(
        f"{realm_name}_org_first_broker_login_confirm_link_execution",
        realm_id=realm_id,
        parent_flow_alias=handle_existing_account_subflow.alias,
        authenticator="idp-confirm-link",
        requirement="REQUIRED",
        priority=10,
        opts=opts,
    )
    account_verification_options_subflow = keycloak.authentication.Subflow(
        f"{realm_name}_org_first_broker_login_account_verification_options_subflow",
        alias="Organization first broker login Account verification options",
        realm_id=realm_id,
        parent_flow_alias=handle_existing_account_subflow.alias,
        provider_id="basic-flow",
        requirement="REQUIRED",
        priority=20,
        opts=opts,
    )
    keycloak.authentication.Execution(
        f"{realm_name}_org_first_broker_login_email_verification_execution",
        realm_id=realm_id,
        parent_flow_alias=account_verification_options_subflow.alias,
        authenticator="idp-email-verification",
        requirement="ALTERNATIVE",
        priority=10,
        opts=opts,
    )
    verify_by_reauth_subflow = keycloak.authentication.Subflow(
        f"{realm_name}_org_first_broker_login_verify_by_reauth_subflow",
        alias=(
            "Organization first broker login Verify Existing Account by"
            " Re-authentication"
        ),
        realm_id=realm_id,
        parent_flow_alias=account_verification_options_subflow.alias,
        provider_id="basic-flow",
        requirement="ALTERNATIVE",
        priority=20,
        opts=opts,
    )
    keycloak.authentication.Execution(
        f"{realm_name}_org_first_broker_login_username_password_execution",
        realm_id=realm_id,
        parent_flow_alias=verify_by_reauth_subflow.alias,
        authenticator="idp-username-password-form",
        requirement="REQUIRED",
        priority=10,
        opts=opts,
    )
    conditional_otp_subflow = keycloak.authentication.Subflow(
        f"{realm_name}_org_first_broker_login_conditional_otp_subflow",
        alias="Organization first broker login First broker login - Conditional OTP",
        realm_id=realm_id,
        parent_flow_alias=verify_by_reauth_subflow.alias,
        provider_id="basic-flow",
        requirement="CONDITIONAL",
        priority=20,
        opts=opts,
    )
    keycloak.authentication.Execution(
        f"{realm_name}_org_first_broker_login_conditional_otp_user_configured_execution",
        realm_id=realm_id,
        parent_flow_alias=conditional_otp_subflow.alias,
        authenticator="conditional-user-configured",
        requirement="REQUIRED",
        priority=10,
        opts=opts,
    )
    keycloak.authentication.Execution(
        f"{realm_name}_org_first_broker_login_conditional_otp_form_execution",
        realm_id=realm_id,
        parent_flow_alias=conditional_otp_subflow.alias,
        authenticator="auth-otp-form",
        requirement="REQUIRED",
        priority=20,
        opts=opts,
    )
    keycloak.authentication.Execution(
        f"{realm_name}_org_first_broker_login_add_org_member_execution",
        realm_id=realm_id,
        parent_flow_alias=main_flow.alias,
        authenticator="idp-add-organization-member",
        requirement="REQUIRED",
        priority=23,
        opts=opts,
    )
    return main_flow
