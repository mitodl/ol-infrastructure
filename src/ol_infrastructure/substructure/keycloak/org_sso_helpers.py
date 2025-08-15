import pulumi
import pulumi_keycloak as keycloak

from ol_infrastructure.substructure.keycloak.saml_helpers import (
    SAML_FRIENDLY_NAMES,
    extract_saml_metadata,
    generate_pulumi_args_dict,
    get_saml_attribute_mappers,
)


def create_org_for_learn(  # noqa: PLR0913
    org_domains: list[str],
    org_name: str,
    org_alias: str,
    learn_domain: str,
    realm_id: str | pulumi.Output[str],
    resource_options: pulumi.ResourceOptions,
) -> keycloak.organization.Organization:
    return keycloak.organization.Organization(
        f"ol-apps-{org_alias}-organization",
        domains=[
            keycloak.organization.OrganizationDomainArgs(name=org_domain, verified=True)
            for org_domain in org_domains
        ],
        enabled=True,
        name=org_name,
        alias=org_alias.lower(),
        redirect_url=f"https://{learn_domain}/dashboard/organization/{org_alias.lower()}",
        realm=realm_id,
        attributes={"slug": org_alias},
        opts=resource_options,
    )


def onboard_saml_org(  # noqa: PLR0913
    org_domains: list[str],
    org_name: str,
    org_alias: str,
    org_saml_metadata_url: str,
    keycloak_url: str,
    learn_domain: str,
    realm_id: str | pulumi.Output[str],
    first_login_flow: pulumi.Output[keycloak.authentication.Flow],
    resource_options: pulumi.ResourceOptions,
):
    org = create_org_for_learn(
        org_domains, org_name, org_alias, learn_domain, realm_id, resource_options
    )

    saml_args = generate_pulumi_args_dict(extract_saml_metadata(org_saml_metadata_url))

    org_idp = keycloak.saml.IdentityProvider(
        f"ol-apps-{org_alias}-saml-idp",
        alias=org_alias.lower(),
        display_name=org_name,
        entity_id=f"{keycloak_url}/realms/olapps",
        first_broker_login_flow_alias=first_login_flow.alias,
        hide_on_login_page=True,
        name_id_policy_format="Unspecified",
        org_domain="ANY",
        org_redirect_mode_email_matches=True,
        organization_id=org.id,
        post_binding_authn_request=True,
        post_binding_response=True,
        realm=realm_id,
        sync_mode="IMPORT",
        trust_email=True,
        validate_signature=True,
        opts=resource_options,
        extra_config={
            "metadataDescriptorUrl": org_saml_metadata_url,
            "useMetadataDescriptorUrl": True,
        },
        **saml_args,
    )
    mappers = get_saml_attribute_mappers(org_saml_metadata_url, org_alias.lower())
    for attr, args in mappers.items():
        keycloak.AttributeImporterIdentityProviderMapper(
            f"map-{org_alias}-saml-{attr}-attribute",
            realm=realm_id,
            identity_provider_alias=org_idp.alias,
            **args,
        )
    if not mappers:
        for attr, friendly_names in SAML_FRIENDLY_NAMES.items():
            for friendly_name in friendly_names:
                keycloak.AttributeImporterIdentityProviderMapper(
                    f"map-{org_alias}-saml-{friendly_name}-attribute",
                    realm=realm_id,
                    attribute_friendly_name=friendly_name,
                    identity_provider_alias=org_idp.alias,
                    user_attribute=attr,
                    extra_config={
                        "syncMode": "INHERIT",
                    },
                    opts=resource_options,
                )
