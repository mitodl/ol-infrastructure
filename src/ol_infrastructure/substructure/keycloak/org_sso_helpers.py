import pulumi
import pulumi_keycloak as keycloak


def onboard_saml_org(  # noqa: PLR0913
    org_domain: str,
    org_name: str,
    org_alias: str,
    org_saml_metadata_url: str,
    keycloak_url: str,
    learn_domain: str,
    realm_id: str | pulumi.Output[str],
    first_login_flow: pulumi.Output[keycloak.authentication.Flow],
    resource_options: pulumi.ResourceOptions,
):
    org = keycloak.organization.Organization(
        f"ol-apps-{org_alias}-organization",
        domains=[
            keycloak.organization.OrganizationDomainArgs(name=org_domain, verified=True)
        ],
        enabled=True,
        name=org_name,
        alias=org_alias.lower(),
        redirect_url=f"https://{learn_domain}/dashboard/organization/{org_alias.lower()}",
        realm=realm_id,
        attributes={"slug": org_alias},
        opts=resource_options,
    )

    org_idp = keycloak.saml.IdentityProvider(
        f"ol-apps-{org_alias}-saml-idp",
        alias=f"{org_alias}",
        display_name=org_name,
        entity_id=f"{keycloak_url}/realms/olapps",
        first_broker_login_flow_alias=first_login_flow.alias,
        hide_on_login_page=True,
        org_domain=org_domain,
        org_redirect_mode_email_matches=True,
        organization_id=org.id,
        post_binding_authn_request=True,
        post_binding_response=True,
        realm=realm_id,
        single_sign_on_service_url=org_saml_metadata_url,
        sync_mode="FORCE",
        trust_email=True,
        validate_signature=True,
        opts=resource_options,
    )
    keycloak.AttributeImporterIdentityProviderMapper(
        f"map-{org_alias}-saml-email-attribute",
        realm=realm_id,
        attribute_friendly_name="mail",
        identity_provider_alias=org_idp.alias,
        user_attribute="email",
        extra_config={
            "syncMode": "INHERIT",
        },
        opts=resource_options,
    )
    keycloak.AttributeImporterIdentityProviderMapper(
        f"map-{org_alias}-saml-last-name-attribute",
        realm=realm_id,
        attribute_friendly_name="sn",
        identity_provider_alias=org_idp.alias,
        user_attribute="lastName",
        extra_config={
            "syncMode": "INHERIT",
        },
        opts=resource_options,
    )
    keycloak.AttributeImporterIdentityProviderMapper(
        f"map-{org_alias}-saml-first-name-attribute",
        realm=realm_id,
        attribute_friendly_name="givenName",
        identity_provider_alias=org_idp.alias,
        user_attribute="firstName",
        extra_config={
            "syncMode": "INHERIT",
        },
        opts=resource_options,
    )
    keycloak.AttributeImporterIdentityProviderMapper(
        f"map-{org_alias}-saml-full-name-attribute",
        realm=realm_id,
        attribute_friendly_name="displayName",
        identity_provider_alias=org_idp.alias,
        user_attribute="fullName",
        extra_config={
            "syncMode": "INHERIT",
        },
        opts=resource_options,
    )
    keycloak.HardcodedAttributeIdentityProviderMapper(
        f"map-{org_alias}-email-opt-in-attribute",
        name="email-opt-in-default",
        realm=realm_id,
        identity_provider_alias=org_idp.alias,
        attribute_name="emailOptIn",
        attribute_value="1",
        user_session=False,
        extra_config={
            "syncMode": "INHERIT",
        },
    )
