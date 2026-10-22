"""A partner's metadata being unavailable must fail the deploy, not delete their IdP.

These helpers used to log a warning and return an empty result, which dropped the
identity-provider resource from the Pulumi program. Pulumi reads that as a deletion
and removes the partner's working SSO integration.
"""

from urllib.error import URLError

import httpx
import pulumi
import pulumi_keycloak as keycloak
import pytest

from ol_infrastructure.substructure.keycloak import oidc_helpers, saml_helpers
from ol_infrastructure.substructure.keycloak.oidc_helpers import (
    OidcDiscoveryError,
    oidc_identity_provider_args_from_discovery_url,
)
from ol_infrastructure.substructure.keycloak.org_sso_helpers import (
    OrgConfig,
    SamlIdpConfig,
    onboard_saml_org,
)
from ol_infrastructure.substructure.keycloak.saml_helpers import (
    SamlMetadataError,
    extract_saml_metadata,
    get_saml_attribute_mappers,
)
from tests.ol_infrastructure.recording_mocks import RecordingMocks

DISCOVERY_URL = "https://idp.example.com/.well-known/openid-configuration"
METADATA_URL = "https://idp.example.com/metadata.xml"
REFUSED = "connection refused"
SAVED_METADATA = """<md:EntityDescriptor
    xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
    xmlns:ds="http://www.w3.org/2000/09/xmldsig#" entityID="idp.example.com">
  <md:IDPSSODescriptor
      protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:KeyDescriptor use="signing"><ds:KeyInfo><ds:X509Data>
      <ds:X509Certificate>MIIBsaved</ds:X509Certificate>
    </ds:X509Data></ds:KeyInfo></md:KeyDescriptor>
    <md:SingleSignOnService
        Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
        Location="https://idp.example.com/sso"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>"""


# Both helper modules bind their fetch function at import time
# (`from urllib.request import urlopen`, `import httpx` then `httpx.get`), so
# every patch here targets the helper module's own attribute. Patching
# urllib.request.urlopen instead leaves saml_helpers.urlopen pointing at the
# original and the test makes a real network call.
@pytest.fixture
def unreachable_saml_endpoint(monkeypatch):
    """Make every SAML metadata fetch fail the way an offline partner IdP does."""

    def _raise(*args, **kwargs):  # noqa: ARG001
        raise URLError(REFUSED)

    monkeypatch.setattr(saml_helpers, "urlopen", _raise)


@pytest.fixture
def unreachable_oidc_endpoint(monkeypatch):
    """Make every OIDC discovery fetch fail the way an offline partner IdP does."""

    def _raise(*args, **kwargs):  # noqa: ARG001
        raise httpx.ConnectError(REFUSED)

    monkeypatch.setattr(oidc_helpers.httpx, "get", _raise)


def _patch_discovery_response(monkeypatch, status, body):
    """Serve one canned discovery response.

    The request has to be attached: raise_for_status() reads it before it looks
    at the status code, and errors out on a response built without one.
    """

    def _respond(*args, **kwargs):  # noqa: ARG001
        return httpx.Response(
            status, json=body, request=httpx.Request("GET", DISCOVERY_URL)
        )

    monkeypatch.setattr(oidc_helpers.httpx, "get", _respond)


@pytest.mark.usefixtures("unreachable_saml_endpoint")
def test_saml_metadata_fetch_failure_raises():
    with pytest.raises(SamlMetadataError, match=f"Unable to fetch or parse.*{REFUSED}"):
        extract_saml_metadata(METADATA_URL)


@pytest.mark.usefixtures("unreachable_saml_endpoint")
def test_saml_attribute_mappers_fetch_failure_raises():
    with pytest.raises(SamlMetadataError, match=f"Unable to fetch or parse.*{REFUSED}"):
        get_saml_attribute_mappers(METADATA_URL, "example")


def test_saml_metadata_url_must_be_https():
    with pytest.raises(SamlMetadataError, match="must use HTTPS"):
        extract_saml_metadata("http://idp.example.com/metadata.xml")


def test_unparseable_saml_metadata_xml_raises():
    with pytest.raises(SamlMetadataError, match="Unable to parse"):
        extract_saml_metadata("<EntityDescriptor>truncated")


@pytest.mark.usefixtures("unreachable_oidc_endpoint")
def test_oidc_discovery_fetch_failure_raises():
    with pytest.raises(OidcDiscoveryError, match=f"Unable to fetch.*{REFUSED}"):
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
    _patch_discovery_response(monkeypatch, 200, metadata)
    with pytest.raises(OidcDiscoveryError, match=expected):
        oidc_identity_provider_args_from_discovery_url(
            DISCOVERY_URL, client_secret=client_secret
        )


@pytest.mark.parametrize("status", [401, 404, 500])
def test_oidc_error_response_with_json_body_raises(monkeypatch, status):
    """An error page that happens to be JSON must not be read as metadata.

    Without a status check this parses cleanly, and a body carrying neither
    endpoints nor token_endpoint_auth_methods_supported takes the same path as a
    provider that legitimately defaults to client_secret_basic - yielding an IdP
    with no authorization or token URL.
    """
    _patch_discovery_response(monkeypatch, status, {"error": "not found"})
    with pytest.raises(OidcDiscoveryError, match="Unable to fetch"):
        oidc_identity_provider_args_from_discovery_url(
            DISCOVERY_URL,
            client_secret="a-secret",  # pragma: allowlist secret
        )


@pytest.mark.usefixtures("unreachable_saml_endpoint")
def test_saved_saml_metadata_deploys_while_the_endpoint_is_down():
    """A saved copy builds the IdP without a fetch and keeps the descriptor URL.

    Keycloak still refreshes keys from the URL, so the live IdP doesn't change
    when a copy is added.
    """
    recording = RecordingMocks()
    pulumi.runtime.set_mocks(recording, preview=False)
    resource_options = pulumi.ResourceOptions()

    @pulumi.runtime.test
    def register():
        onboard_saml_org(
            SamlIdpConfig(
                idp_alias="partner",
                idp_display_name="Partner",
                keycloak_url="https://sso.example.com",
                first_login_flow=keycloak.authentication.Flow(
                    "first-login", realm_id="olapps", alias="first-login"
                ),
                realm_id="olapps",
                resource_options=resource_options,
                org_saml_metadata_url=METADATA_URL,
                org_saml_metadata_xml=SAVED_METADATA,
                principal_type="ATTRIBUTE",
                principal_attribute="mail",
                attribute_name_map={"email": "mail"},
            ),
            OrgConfig(
                org_domains=["example.com"],
                org_name="Partner",
                org_alias="Partner",
                learn_domain="learn.example.com",
                realm_id="olapps",
                resource_options=resource_options,
            ),
        )

    register()
    (idp,) = recording.of_type("keycloak:saml/identityProvider:IdentityProvider")
    assert idp["singleSignOnServiceUrl"] == "https://idp.example.com/sso"
    assert idp["signingCertificate"] == "MIIBsaved"
    assert idp["extraConfig"] == {
        "metadataDescriptorUrl": METADATA_URL,
        "useMetadataDescriptorUrl": True,
    }
