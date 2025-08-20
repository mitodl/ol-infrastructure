"""Helper functions for managing Keycloak SAML integrations."""

import xml.etree.ElementTree as ET
from collections.abc import Collection
from urllib.parse import urlparse
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


def _fetch_and_parse_saml_metadata(metadata_url: str) -> ET.Element | None:
    """Fetch and parse SAML metadata from a URL.

    Args:
        metadata_url: The URL of the SAML IdP metadata XML file.

    Returns:
        The root ElementTree element of the parsed XML, or None if parsing fails.
    """
    parsed_url = urlparse(metadata_url)
    if parsed_url.scheme != "https":
        return None
    try:
        # Set timeout and limit response size
        MAX_METADATA_SIZE = 10 * 1024 * 1024  # 10MB
        with urlopen(metadata_url, timeout=10) as metadata_file:  # noqa: S310
            metadata_bytes = metadata_file.read(MAX_METADATA_SIZE + 1)
            if len(metadata_bytes) > MAX_METADATA_SIZE:
                return None
            return ET.fromstring(metadata_bytes)  # noqa: S314
    except (OSError, ET.ParseError):
        return None


def extract_saml_metadata(metadata_url: str) -> dict[str, str | None]:
    """
    Extract relevant information from a SAML IdP metadata XML file.

    Args:
        metadata_url (str): The URL of the SAML IdP metadata XML file.

    Returns:
        dict: A dictionary containing the extracted metadata attributes,
              or an empty dictionary if parsing fails.
    """
    root = _fetch_and_parse_saml_metadata(metadata_url)
    if root is None:
        return {}

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
    x509_certificate = x509_cert_element.text if x509_cert_element is not None else None

    return {
        "entity_id": entity_id,
        "single_sign_on_service_url": sso_url,
        "single_logout_service_url": slo_url,
        "x509_certificate": x509_certificate.strip() if x509_certificate else None,
    }


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


def get_saml_attribute_mappers(
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
    root = _fetch_and_parse_saml_metadata(metadata_url)
    if root is None:
        return {}

    namespaces = {
        "md": "urn:oasis:names:tc:SAML:2.0:metadata",
        "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    }

    common_friendly_names = SAML_FRIENDLY_NAMES
    mappers = {}

    for (
        keycloak_user_attribute,
        friendly_name_candidates,
    ) in common_friendly_names.items():
        found_attribute_name = None

        # Check for matching Friendly Name first
        for candidate in friendly_name_candidates:
            # Look for any attribute tag with a matching FriendlyName
            attribute_element = root.find(
                f".//*[@FriendlyName='{candidate}']", namespaces
            )
            if attribute_element is not None:
                found_attribute_name = attribute_element.get("Name")
                if not found_attribute_name:  # Sometimes only FriendlyName is present
                    found_attribute_name = candidate
                break  # Found a match, stop searching friendly names

        if found_attribute_name:
            mappers[found_attribute_name] = {
                "name": f"{idp_alias}-{keycloak_user_attribute}-mapper",
                "attribute_name": found_attribute_name,
                "user_attribute": keycloak_user_attribute,
                "extra_config": {
                    "syncMode": "INHERIT",
                    "attribute.name.format": "ATTRIBUTE_FORMAT_URI",
                },
            }

    return mappers
