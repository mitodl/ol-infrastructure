"""Helper functions for managing Keycloak SAML integrations."""

import ssl
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import certifi


class SamlMetadataError(Exception):
    """A SAML IdP's metadata could not be fetched or parsed.

    Raised instead of returning empty metadata so that an unreachable partner
    endpoint fails the deploy. Returning empty dropped the IdP resource from the
    Pulumi program, which Pulumi then deleted along with the working integration.
    """


# Some standalone/pyenv-managed Python builds (e.g. on macOS) don't reliably
# pick up the system trust store via ssl.create_default_context(), causing
# CERTIFICATE_VERIFY_FAILED for otherwise-valid certificates (e.g. issued by
# HARICA). Load the default (system) certs as usual, then layer certifi's
# actively-maintained bundle on top, so both trust sources are honored instead
# of certifi replacing the system/enterprise trust store.
_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.load_verify_locations(cafile=certifi.where())

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
        # https://community.lucid.co/admin-questions-2/azure-saml-sso-and-first-last-names-in_attributes-claims-not-working-7600}
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


def _fetch_and_parse_saml_metadata(metadata_url: str) -> ET.Element:
    """Fetch and parse SAML metadata from a URL.

    Args:
        metadata_url: The URL of the SAML IdP metadata XML file.

    Returns:
        The root ElementTree element of the parsed XML.

    Raises:
        SamlMetadataError: The URL is not HTTPS, the fetch failed, the body
            exceeded the size limit, or the payload was not parseable XML.
    """
    parsed_url = urlparse(metadata_url)
    if parsed_url.scheme != "https":
        msg = f"SAML metadata URL must use HTTPS, got {metadata_url}"
        raise SamlMetadataError(msg)
    try:
        # Set timeout and limit response size
        MAX_METADATA_SIZE = 10 * 1024 * 1024  # 10MB
        # Use a browser-like User-Agent; some IdP endpoints (e.g. behind Cloudflare)
        # return 403 when they see Python's default urllib User-Agent.
        request = Request(  # noqa: S310
            metadata_url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; Keycloak-metadata-fetcher)"
            },
        )
        with urlopen(request, timeout=10, context=_SSL_CONTEXT) as metadata_file:  # noqa: S310
            metadata_bytes = metadata_file.read(MAX_METADATA_SIZE + 1)
            if len(metadata_bytes) > MAX_METADATA_SIZE:
                msg = (
                    f"SAML metadata from {metadata_url} exceeds the "
                    f"{MAX_METADATA_SIZE} byte maximum"
                )
                raise SamlMetadataError(msg)
            return ET.fromstring(metadata_bytes)  # noqa: S314
    except (OSError, ET.ParseError) as e:
        msg = f"Unable to fetch or parse SAML metadata from {metadata_url}: {e}"
        raise SamlMetadataError(msg) from e


def _parse_saml_metadata_string(metadata_xml: str) -> ET.Element:
    """Parse SAML metadata from an XML string.

    Args:
        metadata_xml: The SAML IdP metadata as an XML string.

    Returns:
        The root ElementTree element of the parsed XML.

    Raises:
        SamlMetadataError: The string was not parseable XML.
    """
    try:
        return ET.fromstring(metadata_xml)  # noqa: S314
    except ET.ParseError as e:
        msg = f"Unable to parse the supplied SAML metadata XML: {e}"
        raise SamlMetadataError(msg) from e


def extract_saml_metadata(metadata_source: str) -> dict[str, str | None]:
    """
    Extract relevant information from a SAML IdP metadata XML file or string.

    Args:
        metadata_source (str): Either the URL of the SAML IdP metadata XML file,
                               or the XML string itself.

    Returns:
        dict: A dictionary containing the extracted metadata attributes.

    Raises:
        SamlMetadataError: The metadata could not be fetched or parsed.
    """
    # Determine if this is a URL or XML string
    if metadata_source.strip().startswith(
        "<?xml"
    ) or metadata_source.strip().startswith("<"):
        root = _parse_saml_metadata_string(metadata_source)
    else:
        root = _fetch_and_parse_saml_metadata(metadata_source)

    # Define namespaces
    namespaces = {
        "md": "urn:oasis:names:tc:SAML:2.0:metadata",
        "ds": "http://www.w3.org/2000/09/xmldsig#",
    }

    # Extract Entity ID
    entity_id = root.get("entityID")

    # Extract Single Sign-On Service URL
    # Look for SAML2.0 POST binding endpoints first.
    sso_service = root.find(
        ".//md:SingleSignOnService[@Binding='urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST']",
        namespaces,
    )
    # If no POST binding, look for any SAML2.0 binding
    if sso_service is None:
        for service in root.findall(".//md:SingleSignOnService", namespaces):
            binding = service.get("Binding")
            if binding and binding.startswith("urn:oasis:names:tc:SAML:2.0:bindings:"):
                sso_service = service
                break

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


def generate_pulumi_args_dict(metadata: dict[str, str | None]) -> dict[str, str]:
    """Generate a dictionary of arguments for the Pulumi IdentityProvider resource.

    Args:
        metadata (dict): Dictionary containing extracted IdP metadata.

    Returns: dict: A dictionary of arguments suitable for Pulumi. Keys absent from
        the metadata are omitted so that a caller's explicit override still applies.

    """
    args_dict: dict[str, str] = {}

    sso_url = metadata.get("single_sign_on_service_url")
    if sso_url:
        args_dict["single_sign_on_service_url"] = sso_url

    slo_url = metadata.get("single_logout_service_url")
    if slo_url:
        args_dict["single_logout_service_url"] = slo_url

    cert = metadata.get("x509_certificate")
    if cert:
        args_dict["signing_certificate"] = cert

    return args_dict


def get_saml_attribute_mappers(  # noqa: C901, PLR0912
    metadata_source: str,
    idp_alias: str,
    attribute_map: dict[str, str] | None = None,
    attribute_name_map: dict[str, str] | None = None,
    mapper_extra_config: dict[str, dict[str, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Parse SAML metadata to find attributes that can be used for attribute mappers.

    It first attempts to find attributes by their "FriendlyName" and falls back to a
    list of candidate attribute names.

    Args:
        metadata_source: Either the URL to the SAML IdP metadata XML or the
            XML string itself.
        idp_alias: The alias for the Keycloak identity provider.
        attribute_map: Optional mapping of Keycloak user attributes to SAML
            friendly names.
        attribute_name_map: Optional mapping of Keycloak user attributes to SAML
            attribute names (not friendly names).
        mapper_extra_config: Optional per-attribute extra configuration overrides.

    Returns:
        A dictionary of attribute mapper configurations suitable for Pulumi.

    Raises:
        SamlMetadataError: The metadata could not be fetched or parsed.

    """
    # Determine if this is a URL or XML string
    if metadata_source.strip().startswith(
        "<?xml"
    ) or metadata_source.strip().startswith("<"):
        root = _parse_saml_metadata_string(metadata_source)
    else:
        root = _fetch_and_parse_saml_metadata(metadata_source)

    namespaces = {
        "md": "urn:oasis:names:tc:SAML:2.0:metadata",
        "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    }

    common_friendly_names = SAML_FRIENDLY_NAMES
    mappers = {}

    # Handle attribute_name_map (takes precedence over attribute_map)
    if attribute_name_map:
        for mapped_attribute, attribute_name in attribute_name_map.items():
            extra_config = {
                "syncMode": "INHERIT",
                "attribute.name.format": "ATTRIBUTE_FORMAT_URI",
            }
            # Apply per-attribute extra config overrides if provided
            if mapper_extra_config and mapped_attribute in mapper_extra_config:
                extra_config.update(mapper_extra_config[mapped_attribute])

            mappers[mapped_attribute] = {
                "name": f"{idp_alias}-{mapped_attribute}-mapper",
                "attribute_name": attribute_name,
                "user_attribute": mapped_attribute,
                "extra_config": extra_config,
            }
        return mappers

    # Handle attribute_map (friendly names)
    if attribute_map:
        for mapped_attribute, friendly_name in attribute_map.items():
            extra_config = {
                "syncMode": "INHERIT",
                "attribute.name.format": "ATTRIBUTE_FORMAT_URI",
            }
            # Apply per-attribute extra config overrides if provided
            if mapper_extra_config and mapped_attribute in mapper_extra_config:
                extra_config.update(mapper_extra_config[mapped_attribute])

            mappers[mapped_attribute] = {
                "name": f"{idp_alias}-{mapped_attribute}-mapper",
                "attribute_friendly_name": friendly_name,
                "user_attribute": mapped_attribute,
                "extra_config": extra_config,
            }
        return mappers

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
