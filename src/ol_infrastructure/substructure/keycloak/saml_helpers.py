import xml.etree.ElementTree as ET
from collections.abc import Collection
from urllib.request import urlopen

SAML_FRIENDLY_NAMES = {
    "firstName": [
        "Given Name",
        "GivenName",
        "First Name",
        "first_name",
        "firstName",  # Sometimes used as FriendlyName
    ],
    "lastName": [
        "Surname",
        "sn",  # Often the attribute Name, but sometimes FriendlyName
        "Last Name",
        "last_name",
        "lastName",  # Sometimes used as FriendlyName
    ],
    "email": [
        "E-Mail Address",
        "mail",  # Often the attribute Name, but sometimes FriendlyName
        "Email",
        "emailaddress",
        "Email Address",
        "user.email",  # {Link: According to Lucid Community
        # https://community.lucid.co/admin-questions-2/azure-saml-sso-and-first-last-names-in-attributes-claims-not-working-7600}
    ],
    "fullName": [
        "Name",  # Often used in ADFS for the display name or full name
        "Display Name",
        "displayName",
        "cn",  # Common Name (often includes full name)
        "Common Name or Full Name",
        "FullName",  # {Link: According to Autodesk
        # https://help.autodesk.com/view/SSOGUIDE/ENU/?guid=SSOGUIDE_Okta_Guide_About_Single_Sign_on_SSO_Frequently_Asked_Questions_FAQ_What_are_attribute_names_html}
    ],
    "username": [
        "Name ID",
        "Name Identifier",
        "User Principal Name",  # ADFS/Azure AD
        "UserPrincipalName",
        "NameID",  # Often attribute name, but sometimes friendly name
        "sAMAccountName",
        "username",
        "uid",
    ],
}


def extract_saml_metadata(metadata_url: str) -> dict[str, str | None]:
    """
    Extract relevant information from a SAML IdP metadata XML file.

    Args:
        metadata_file (str): Path to the SAML IdP metadata XML file.

    Returns:
        dict: A dictionary containing the extracted metadata attributes,
              or None if parsing fails.
    """
    try:
        with urlopen(metadata_url) as metadata_file:  # noqa: S310
            tree = ET.parse(metadata_file)  # noqa: S314
            root = tree.getroot()

            # Define namespaces
            namespaces = {
                "md": "urn:oasis:names:tc:SAML:2.0:metadata",
                "ds": "http://www.w3.org/2000/09/xmldsig#",
            }

            # Extract Entity ID
            entity_id = root.get("entityID")

            # Extract Single Sign-On Service URL
            sso_service = root.find(".//md:SingleSignOnService", namespaces)
            sso_url = sso_service.get("Location") if sso_service is not None else None

            # Extract Single Logout Service URL (optional)
            slo_service = root.find(".//md:SingleLogoutService", namespaces)
            slo_url = slo_service.get("Location") if slo_service is not None else None

            # Extract X.509 Certificate (signing certificate)
            x509_cert_element = root.find(
                ".//md:KeyDescriptor[@use='signing']//ds:X509Certificate", namespaces
            )
            x509_certificate = (
                x509_cert_element.text if x509_cert_element is not None else None
            )

            return {
                "entity_id": entity_id,
                "single_sign_on_service_url": sso_url,
                "single_logout_service_url": slo_url,
                "x509_certificate": x509_certificate.strip()
                if x509_certificate
                else None,
            }

    except ET.ParseError:
        return {}
    except Exception:  # noqa: BLE001
        return {}


def generate_pulumi_args_dict(metadata: dict[str, str]) -> dict[str, str]:
    """Generate a dictionary of arguments for the Pulumi IdentityProvider resource.

    Args:
        metadata (dict): Dictionary containing extracted IdP metadata.

    Returns: dict: A dictionary of arguments suitable for Pulumi, or None if metadata is
        missing.

    """
    if not metadata:
        return {}

    args_dict = {
        "single_sign_on_service_url": metadata["single_sign_on_service_url"],
    }

    if metadata["single_logout_service_url"]:
        args_dict["single_logout_service_url"] = metadata["single_logout_service_url"]

    if metadata["x509_certificate"]:
        args_dict["signing_certificate"] = metadata["x509_certificate"]

    return args_dict


def get_saml_attribute_mappers(  # noqa: C901, PLR0912
    metadata_url: str, idp_alias: str
) -> dict[str, dict[str, Collection[str]]]:
    """Parse SAML metadata to find attributes that can be used for attribute mappers.

    It first attempts to find attributes by their "FriendlyName" and falls back to a
    list of candidate attribute names.

    Args:
        metadata_url: The URL to the SAML IdP metadata XML.
        idp_alias: The alias for the Keycloak identity provider.

    Returns:
        A dictionary of attribute mapper configurations suitable for Pulumi.

    """
    try:
        with urlopen(metadata_url) as metadata_file:  # noqa: S310
            tree = ET.parse(metadata_file)  # noqa: S314
            root = tree.getroot()
            namespaces = {
                "md": "urn:oasis:names:tc:SAML:2.0:metadata",
                "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
            }
    except ET.ParseError:
        return {}
    except Exception:  # noqa: BLE001
        return {}

    attribute_mapping_candidates = {
        "email": [
            "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
            "urn:oid:0.9.2342.19200300.100.1.3",
            "email",
            "mail",
        ],
        "firstName": [
            "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname",
            "urn:oid:2.5.4.42",
            "givenName",
            "firstName",
        ],
        "lastName": [
            "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname",
            "urn:oid:2.5.4.4",
            "lastName",
            "sn",
            "surname",
        ],
        "fullName": [
            # ADFS "Name" claim for display name
            "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
            "displayName",  # Common FriendlyName
        ],
        "username": [
            "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
            "urn:oasis:names:tc:SAML:attribute-def:uid",
            "sAMAccountName",
            "NameID",
            "username",
        ],
    }

    common_friendly_names = SAML_FRIENDLY_NAMES
    mappers = {}

    for (
        keycloak_user_attribute,
        saml_attribute_candidates,
    ) in attribute_mapping_candidates.items():
        found_attribute_name = None

        # Check for matching Friendly Name first
        if keycloak_user_attribute in common_friendly_names:
            friendly_name_candidates = common_friendly_names[keycloak_user_attribute]
            for candidate in friendly_name_candidates:
                # Look for any attribute tag with a matching FriendlyName
                attribute_element = root.find(
                    f".//*[@FriendlyName='{candidate}']", namespaces
                )
                if attribute_element is not None:
                    found_attribute_name = attribute_element.get("Name")
                    if (
                        not found_attribute_name
                    ):  # Sometimes only FriendlyName is present
                        found_attribute_name = candidate
                    break  # Found a match, stop searching friendly names

            if found_attribute_name:
                mapper_args = {
                    "name": f"{idp_alias}-{keycloak_user_attribute}-mapper",
                    "attribute_name": found_attribute_name,
                    "user_attribute": keycloak_user_attribute,
                    "extra_config": {
                        "syncMode": "INHERIT",
                        "attribute.name.format": "ATTRIBUTE_FORMAT_URI",
                    },
                }
                mappers[found_attribute_name] = mapper_args

        # If not found, try by attribute name from candidates
        if not found_attribute_name:
            for candidate in saml_attribute_candidates:
                if (
                    root.find(
                        f".//saml:AttributeStatement/saml:Attribute[@Name='{candidate}']",
                        namespaces,
                    )
                    is not None
                ):
                    found_attribute_name = candidate
                    break
                if (
                    root.find(
                        ".//md:AttributeConsumingService/md:RequestedAttribute"
                        f"[@Name='{candidate}']",
                        namespaces,
                    )
                    is not None
                ):
                    found_attribute_name = candidate
                    break
                if (
                    root.find(
                        ".//md:AttributeConsumingService/md:RequestedAttribute"
                        f"[@FriendlyName='{candidate}']",
                        namespaces,
                    )
                    is not None
                ):
                    found_attribute_name = candidate
                    break

            if found_attribute_name:
                mapper_args = {
                    "name": f"{idp_alias}-{keycloak_user_attribute}-mapper",
                    "attribute_name": found_attribute_name,
                    "user_attribute": keycloak_user_attribute,
                    "extra_config": {
                        "syncMode": "INHERIT",
                        "attribute.name.format": "ATTRIBUTE_FORMAT_URI",
                    },
                }
                mappers[found_attribute_name] = mapper_args

    return mappers
