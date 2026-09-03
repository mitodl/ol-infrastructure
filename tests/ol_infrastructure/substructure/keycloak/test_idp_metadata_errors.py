"""A partner's metadata being unavailable must fail the deploy, not delete their IdP.

These helpers used to log a warning and return an empty result, which dropped the
identity-provider resource from the Pulumi program. Pulumi reads that as a deletion
and removes the partner's working SSO integration.
"""

import urllib.request
from urllib.error import URLError

import httpx
import pytest

from ol_infrastructure.substructure.keycloak.oidc_helpers import (
    OidcDiscoveryError,
    oidc_identity_provider_args_from_discovery_url,
)
from ol_infrastructure.substructure.keycloak.saml_helpers import (
    SamlMetadataError,
    extract_saml_metadata,
    get_saml_attribute_mappers,
)

DISCOVERY_URL = "https://idp.example.com/.well-known/openid-configuration"
METADATA_URL = "https://idp.example.com/metadata.xml"
REFUSED = "connection refused"


@pytest.fixture
def unreachable_saml_endpoint(monkeypatch):
    """Make every SAML metadata fetch fail the way an offline partner IdP does."""

    def _raise(*args, **kwargs):  # noqa: ARG001
        raise URLError(REFUSED)

    monkeypatch.setattr(urllib.request, "urlopen", _raise)


@pytest.fixture
def unreachable_oidc_endpoint(monkeypatch):
    """Make every OIDC discovery fetch fail the way an offline partner IdP does."""

    def _raise(*args, **kwargs):  # noqa: ARG001
        raise httpx.ConnectError(REFUSED)

    monkeypatch.setattr(httpx, "get", _raise)


@pytest.mark.usefixtures("unreachable_saml_endpoint")
def test_saml_metadata_fetch_failure_raises():
    with pytest.raises(SamlMetadataError, match="Unable to fetch or parse"):
        extract_saml_metadata(METADATA_URL)


@pytest.mark.usefixtures("unreachable_saml_endpoint")
def test_saml_attribute_mappers_fetch_failure_raises():
    with pytest.raises(SamlMetadataError, match="Unable to fetch or parse"):
        get_saml_attribute_mappers(METADATA_URL, "example")


def test_saml_metadata_url_must_be_https():
    with pytest.raises(SamlMetadataError, match="must use HTTPS"):
        extract_saml_metadata("http://idp.example.com/metadata.xml")


def test_unparseable_saml_metadata_xml_raises():
    with pytest.raises(SamlMetadataError, match="Unable to parse"):
        extract_saml_metadata("<EntityDescriptor>truncated")


@pytest.mark.usefixtures("unreachable_oidc_endpoint")
def test_oidc_discovery_fetch_failure_raises():
    with pytest.raises(OidcDiscoveryError, match="Unable to fetch"):
        oidc_identity_provider_args_from_discovery_url(DISCOVERY_URL)


@pytest.mark.parametrize(
    ("metadata", "client_secret", "expected"),
    [
        (
            {
                "scopes_supported": ["openid", "email"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic"],
            },
            "a-secret",  # pragma: allowlist secret
            "does not support required scopes",
        ),
        (
            {
                "scopes_supported": ["openid", "email", "profile"],
                "token_endpoint_auth_methods_supported": ["private_key_jwt"],
            },
            "a-secret",  # pragma: allowlist secret
            "client_secret_basic",
        ),
        (
            {
                "scopes_supported": ["openid", "email", "profile"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic"],
            },
            None,
            "private_key_jwt",
        ),
    ],
)
def test_unusable_oidc_provider_raises(monkeypatch, metadata, client_secret, expected):
    def _respond(*args, **kwargs):  # noqa: ARG001
        return httpx.Response(200, json=metadata)

    monkeypatch.setattr(httpx, "get", _respond)
    with pytest.raises(OidcDiscoveryError, match=expected):
        oidc_identity_provider_args_from_discovery_url(
            DISCOVERY_URL, client_secret=client_secret
        )
