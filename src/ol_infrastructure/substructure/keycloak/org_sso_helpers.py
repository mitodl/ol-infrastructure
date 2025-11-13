from enum import Enum
from typing import Literal

import pulumi
import pulumi_keycloak as keycloak
from pydantic import BaseModel, ConfigDict, model_validator

from ol_infrastructure.substructure.keycloak.oidc_helpers import (
    oidc_identity_provider_args_from_discovery_url,
)
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
    org_saml_metadata_url: str | None = None
    org_saml_metadata_xml: str | None = None
    keycloak_url: str
    first_login_flow: keycloak.authentication.Flow
    name_id_format: NameIdFormat = NameIdFormat.unspecified
    principal_type: Literal["SUBJECT", "ATTRIBUTE", "FRIENDLY_ATTRIBUTE"] = "SUBJECT"
    principal_attribute: str | None = None
    mapper_attribute_format: AttributeFormat = AttributeFormat.uri
    attribute_map: dict[str, str] | None = None
    want_assertions_encrypted: bool = False
    want_assertions_signed: bool | None = None  # Optional, no default

    @model_validator(mode="after")
    def ensure_principal_types(self):
        if self.principal_type == "ATTRIBUTE" and self.principal_attribute is None:
            msg = (
                "If using an attribute as the principal type you must set the "
                "principal attribute."
            )
            raise ValueError(msg)
        if not self.org_saml_metadata_url and not self.org_saml_metadata_xml:
            msg = (
                "Either org_saml_metadata_url or org_saml_metadata_xml must be provided"
            )
            raise ValueError(msg)
        if self.org_saml_metadata_url and self.org_saml_metadata_xml:
            msg = (
                "Only one of org_saml_metadata_url or org_saml_metadata_xml "
                "should be provided"
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
        description=org_config.org_name,
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

    metadata_source = (
        saml_config.org_saml_metadata_xml or saml_config.org_saml_metadata_url
    )
    if metadata_source is None:  # Type guard, should not happen due to validation
        pulumi.log.error(f"No metadata source configured for {saml_config.org_alias}")
        return
    saml_metadata = extract_saml_metadata(metadata_source)
    if not saml_metadata:
        pulumi.log.warn(
            f"Skipping SAML IdP creation for {saml_config.org_alias} due to "
            f"inaccessible metadata source"
        )
        return
    saml_args = generate_pulumi_args_dict(saml_metadata)
    mappers = get_saml_attribute_mappers(
        metadata_source,
        saml_config.org_alias.lower(),
        saml_config.attribute_map,
    )

    # Build extra_config based on whether URL or XML is provided
    extra_config = {}
    if saml_config.org_saml_metadata_url:
        extra_config = {
            "metadataDescriptorUrl": saml_config.org_saml_metadata_url,
            "useMetadataDescriptorUrl": True,
        }

    # Build kwargs conditionally to avoid passing None values
    idp_kwargs = {
        "alias": saml_config.org_alias.lower(),
        "display_name": saml_config.org_name,
        "entity_id": f"{saml_config.keycloak_url}/realms/olapps",
        "first_broker_login_flow_alias": saml_config.first_login_flow.alias,
        "hide_on_login_page": True,
        "name_id_policy_format": saml_config.name_id_format,
        "org_domain": "ANY",
        "org_redirect_mode_email_matches": True,
        "organization_id": org.id,
        "post_binding_authn_request": True,
        "post_binding_response": True,
        "principal_type": saml_config.principal_type,
        "principal_attribute": saml_config.principal_attribute,
        "realm": saml_config.realm_id,
        "login_hint": True,
        "sync_mode": "FORCE",
        "trust_email": True,
        "validate_signature": True,
        "want_assertions_encrypted": saml_config.want_assertions_encrypted,
        "opts": saml_config.resource_options,
        "extra_config": extra_config,
        **saml_args,
    }

    # Only add want_assertions_signed if explicitly set
    if saml_config.want_assertions_signed is not None:
        idp_kwargs["want_assertions_signed"] = saml_config.want_assertions_signed

    org_idp = keycloak.saml.IdentityProvider(
        f"ol-apps-{saml_config.org_alias}-saml-idp",
        **idp_kwargs,
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


class OIDCIdpConfig(OrgConfig):
    org_oidc_metadata_url: str
    keycloak_url: str
    first_login_flow: keycloak.authentication.Flow
    client_id: str
    client_secret: str | None = None


def onboard_oidc_org(
    oidc_config: OIDCIdpConfig,
) -> None:
    org = create_org_for_learn(oidc_config)

    oidc_idp_arg_map = oidc_identity_provider_args_from_discovery_url(
        oidc_config.org_oidc_metadata_url,
        client_secret=oidc_config.client_secret,
    )
    if oidc_idp_arg_map is None:
        pulumi.log.warn(
            f"Skipping OIDC IdP creation for {oidc_config.org_alias} due to "
            f"inaccessible metadata URL"
        )
        return
    oidc_idp_arg_map["extra_config"] = {
        "jwtX509HeadersEnabled": True,
    } | oidc_idp_arg_map.get("extra_config", {})
    keycloak.oidc.IdentityProvider(
        f"ol-apps-{oidc_config.org_alias}-oidc-idp",
        alias=oidc_config.org_alias.lower(),
        client_id=oidc_config.client_id,
        first_broker_login_flow_alias=oidc_config.first_login_flow.alias,
        realm=oidc_config.realm_id,
        display_name=oidc_config.org_name,
        enabled=True,
        login_hint=True,
        sync_mode="FORCE",
        hide_on_login_page=True,
        org_domain="ANY",
        org_redirect_mode_email_matches=True,
        organization_id=org.id,
        validate_signature=True,
        trust_email=True,
        **oidc_idp_arg_map,
    )
