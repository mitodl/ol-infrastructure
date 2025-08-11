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
        alias=org_alias,
        redirect_url=f"https://{learn_domain}/dashboard/organization/{org_alias}",
        realm=realm_id,
        opts=resource_options,
    )

    keycloak.saml.IdentityProvider(
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
