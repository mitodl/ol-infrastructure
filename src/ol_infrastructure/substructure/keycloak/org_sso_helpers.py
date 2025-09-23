from enum import Enum
from typing import Literal

import pulumi
import pulumi_keycloak as keycloak
from pydantic import BaseModel, ConfigDict, model_validator

from ol_infrastructure.substructure.keycloak.saml_helpers import (
    SAML_FRIENDLY_NAMES,
    extract_saml_metadata,
    generate_pulumi_args_dict,
    get_saml_attribute_mappers,
)


class NameIdFormat(str, Enum):
    unspecified = "Unspecified"
    email = "Email"
    persistent = "Persistent"
    transient = "Transient"


class AttributeFormat(str, Enum):
    basic = "ATTRIBUTE_FORMAT_BASIC"
    uri = "ATTRIBUTE_FORMAT_URI"


class OrgConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    org_domains: list[str]
    org_name: str
    org_alias: str
    learn_domain: str
    attribute_format: AttributeFormat = AttributeFormat.basic
    realm_id: str | pulumi.Output[str]
    resource_options: pulumi.ResourceOptions


class SamlIdpConfig(OrgConfig):
    org_saml_metadata_url: str
    keycloak_url: str
    first_login_flow: keycloak.authentication.Flow
    name_id_format: NameIdFormat = NameIdFormat.unspecified
    principal_type: Literal["SUBJECT", "ATTRIBUTE", "FRIENDLY_ATTRIBUTE"] = "SUBJECT"
    principal_attribute: str | None = None
    mapper_attribute_format: AttributeFormat = AttributeFormat.uri
    attribute_map: dict[str, str] | None = None

    @model_validator(mode="after")
    def ensure_principal_types(self):
        if self.principal_type == "ATTRIBUTE" and self.principal_attribute is None:
            msg = (
                "If using an attribute as the principal type you must set the "
                "principal attribute."
            )
            raise ValueError(msg)
        return self


def create_org_for_learn(org_config: OrgConfig) -> keycloak.Organization:
    return keycloak.Organization(
        f"ol-apps-{org_config.org_alias}-organization",
        domains=[
            keycloak.organization.OrganizationDomainArgs(name=org_domain, verified=True)
            for org_domain in org_config.org_domains
        ],
        enabled=True,
        name=org_config.org_name,
        alias=org_config.org_alias.lower(),
        redirect_url=f"https://{org_config.learn_domain}/dashboard/organization/{org_config.org_alias.lower()}",
        realm=org_config.realm_id,
        attributes={"slug": org_config.org_alias},
        opts=org_config.resource_options,
    )


def onboard_saml_org(
    saml_config: SamlIdpConfig,
) -> None:
    org = create_org_for_learn(saml_config)

    saml_args = generate_pulumi_args_dict(
        extract_saml_metadata(saml_config.org_saml_metadata_url)
    )
    mappers = get_saml_attribute_mappers(
        saml_config.org_saml_metadata_url,
        saml_config.org_alias.lower(),
        saml_config.attribute_map,
    )
    org_idp = keycloak.saml.IdentityProvider(
        f"ol-apps-{saml_config.org_alias}-saml-idp",
        alias=saml_config.org_alias.lower(),
        display_name=saml_config.org_name,
        entity_id=f"{saml_config.keycloak_url}/realms/olapps",
        first_broker_login_flow_alias=saml_config.first_login_flow.alias,
        hide_on_login_page=True,
        name_id_policy_format=saml_config.name_id_format,
        org_domain="ANY",
        org_redirect_mode_email_matches=True,
        organization_id=org.id,
        post_binding_authn_request=True,
        post_binding_response=True,
        principal_type=saml_config.principal_type,
        principal_attribute=saml_config.principal_attribute,
        realm=saml_config.realm_id,
        sync_mode="IMPORT",
        trust_email=True,
        validate_signature=True,
        opts=saml_config.resource_options,
        extra_config={
            "metadataDescriptorUrl": saml_config.org_saml_metadata_url,
            "useMetadataDescriptorUrl": True,
        },
        **saml_args,
    )
    for attr, args in mappers.items():
        keycloak.AttributeImporterIdentityProviderMapper(
            f"map-{saml_config.org_alias}-saml-{attr}-attribute",
            realm=saml_config.realm_id,
            identity_provider_alias=org_idp.alias,
            **args,
            opts=saml_config.resource_options,
        )
    if not mappers:
        for attr, friendly_names in SAML_FRIENDLY_NAMES.items():
            for friendly_name in friendly_names:
                keycloak.AttributeImporterIdentityProviderMapper(
                    f"map-{saml_config.org_alias}-saml-{friendly_name}-attribute",
                    realm=saml_config.realm_id,
                    attribute_friendly_name=friendly_name,
                    identity_provider_alias=org_idp.alias,
                    user_attribute=attr,
                    extra_config={
                        "syncMode": "INHERIT",
                        "attribute.name.format": saml_config.mapper_attribute_format,
                    },
                    opts=saml_config.resource_options,
                )
