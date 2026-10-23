"""A partner's metadata being unavailable must not delete their IdP.

These helpers used to log a warning and return an empty result, which dropped the
identity-provider resource from the Pulumi program. Pulumi reads that as a deletion
and removes the partner's working SSO integration. With a saved copy the IdP is
built from the copy and the run warns; without one the deploy fails.
"""

import asyncio
import io
import json
import xml.etree.ElementTree as ET
from http.client import BadStatusLine
from pathlib import Path
from urllib.error import URLError

import httpx
import pulumi
import pulumi_keycloak as keycloak
import pytest

# Python 3.14+ compatibility: ensure event loop exists for set_mocks()
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ol_infrastructure.substructure.keycloak import oidc_helpers, olapps, saml_helpers
from ol_infrastructure.substructure.keycloak.oidc_helpers import (
    OidcDiscoveryError,
    load_oidc_discovery,
    oidc_identity_provider_args_from_discovery,
)
from ol_infrastructure.substructure.keycloak.org_sso_helpers import (
    OIDCIdpConfig,
    OrgConfig,
    SamlIdpConfig,
    onboard_oidc_org,
    onboard_saml_org,
)
from ol_infrastructure.substructure.keycloak.saml_helpers import (
    SamlMetadataError,
    extract_saml_metadata,
    load_saml_metadata,
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
SAVED_DISCOVERY = {
    "issuer": "https://idp.example.com",
    "authorization_endpoint": "https://idp.example.com/auth",
    "token_endpoint": "https://idp.example.com/token",
    "jwks_uri": "https://idp.example.com/jwks",
    "scopes_supported": ["openid", "email", "profile"],
    "token_endpoint_auth_methods_supported": ["private_key_jwt"],
}


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


@pytest.fixture
def warnings(monkeypatch):
    """Collect what the helpers pass to pulumi.log.warn."""
    collected: list[str] = []
    monkeypatch.setattr(pulumi.log, "warn", collected.append)
    return collected


def _serve_saml_metadata(monkeypatch, xml):
    def _respond(*args, **kwargs):  # noqa: ARG001
        return io.BytesIO(xml.encode())

    monkeypatch.setattr(saml_helpers, "urlopen", _respond)


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
def test_saml_metadata_fetch_failure_without_saved_copy_raises():
    with pytest.raises(SamlMetadataError, match=f"Unable to fetch or parse.*{REFUSED}"):
        load_saml_metadata(METADATA_URL, None)


@pytest.mark.usefixtures("unreachable_saml_endpoint")
def test_saml_metadata_fetch_failure_falls_back_to_saved_copy(warnings):
    root = load_saml_metadata(METADATA_URL, SAVED_METADATA)
    assert root.get("entityID") == "idp.example.com"
    assert len(warnings) == 1
    assert REFUSED in warnings[0]
    assert "saved copy" in warnings[0]


def test_saml_metadata_prefers_live_and_warns_on_drift(monkeypatch, warnings):
    _serve_saml_metadata(
        monkeypatch, SAVED_METADATA.replace("MIIBsaved", "MIIBrotated")
    )
    root = load_saml_metadata(METADATA_URL, SAVED_METADATA)
    assert "MIIBrotated" in ET.tostring(root, encoding="unicode")
    assert len(warnings) == 1
    assert "x509_certificate" in warnings[0]


def test_malformed_saml_response_falls_back_to_saved_copy(monkeypatch, warnings):
    """A bad status line reaches us as HTTPException, not URLError."""

    def _raise(*args, **kwargs):  # noqa: ARG001
        status_line = "garbage"
        raise BadStatusLine(status_line)

    monkeypatch.setattr(saml_helpers, "urlopen", _raise)
    assert load_saml_metadata(METADATA_URL, SAVED_METADATA).get("entityID")
    assert len(warnings) == 1


def test_non_metadata_xml_falls_back_to_saved_copy(monkeypatch, warnings):
    _serve_saml_metadata(monkeypatch, "<html><body>Maintenance</body></html>")
    assert load_saml_metadata(METADATA_URL, SAVED_METADATA).get("entityID")
    assert len(warnings) == 1
    assert "not an EntityDescriptor" in warnings[0]


def test_saml_metadata_warns_when_attributes_drift(monkeypatch, warnings):
    """Without an attribute map, the advertised attributes decide the mappers."""
    attribute = (
        '<saml:Attribute xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        'Name="mail" FriendlyName="Email"/>'
    )
    _serve_saml_metadata(
        monkeypatch,
        SAVED_METADATA.replace(
            "</md:IDPSSODescriptor>", f"{attribute}</md:IDPSSODescriptor>"
        ),
    )
    load_saml_metadata(METADATA_URL, SAVED_METADATA)
    assert len(warnings) == 1
    assert "attributes" in warnings[0]


def test_saml_metadata_matching_saved_copy_does_not_warn(monkeypatch, warnings):
    _serve_saml_metadata(monkeypatch, SAVED_METADATA)
    load_saml_metadata(METADATA_URL, SAVED_METADATA)
    assert warnings == []


def test_saml_metadata_url_must_be_https():
    """A saved copy doesn't mask a misconfigured URL."""
    with pytest.raises(SamlMetadataError, match="must use HTTPS"):
        load_saml_metadata("http://idp.example.com/metadata.xml", SAVED_METADATA)


def test_saved_saml_copy_that_is_not_metadata_raises():
    with pytest.raises(SamlMetadataError, match="not an EntityDescriptor"):
        load_saml_metadata(None, "<html><body>Maintenance</body></html>")


def test_unparseable_saml_metadata_xml_raises():
    with pytest.raises(SamlMetadataError, match="Unable to parse"):
        load_saml_metadata(None, "<EntityDescriptor>truncated")


@pytest.mark.usefixtures("unreachable_oidc_endpoint")
def test_oidc_discovery_fetch_failure_without_saved_copy_raises():
    with pytest.raises(OidcDiscoveryError, match=f"Unable to fetch.*{REFUSED}"):
        load_oidc_discovery(DISCOVERY_URL, None)


@pytest.mark.usefixtures("unreachable_oidc_endpoint")
def test_oidc_discovery_fetch_failure_falls_back_to_saved_copy(warnings):
    assert (
        load_oidc_discovery(DISCOVERY_URL, json.dumps(SAVED_DISCOVERY))
        == SAVED_DISCOVERY
    )
    assert len(warnings) == 1
    assert REFUSED in warnings[0]


def test_oidc_discovery_prefers_live_and_warns_on_drift(monkeypatch, warnings):
    live = SAVED_DISCOVERY | {"token_endpoint": "https://idp.example.com/token/v2"}
    _patch_discovery_response(monkeypatch, 200, live)
    assert load_oidc_discovery(DISCOVERY_URL, json.dumps(SAVED_DISCOVERY)) == live
    assert len(warnings) == 1
    assert "token_endpoint" in warnings[0]


@pytest.mark.parametrize("body", [["not", "a", "document"], {"status": "maintenance"}])
def test_non_discovery_json_falls_back_to_saved_copy(monkeypatch, warnings, body):
    _patch_discovery_response(monkeypatch, 200, body)
    assert (
        load_oidc_discovery(DISCOVERY_URL, json.dumps(SAVED_DISCOVERY))
        == SAVED_DISCOVERY
    )
    assert len(warnings) == 1


def test_unparseable_saved_oidc_copy_raises(monkeypatch):
    """Raised even when the live fetch succeeds, since the drift check reads it."""
    _patch_discovery_response(monkeypatch, 200, SAVED_DISCOVERY)
    with pytest.raises(OidcDiscoveryError, match="Unable to parse the saved"):
        load_oidc_discovery(DISCOVERY_URL, '{"issuer": ')


def test_oidc_discovery_matching_saved_copy_does_not_warn(monkeypatch, warnings):
    _patch_discovery_response(monkeypatch, 200, SAVED_DISCOVERY)
    load_oidc_discovery(DISCOVERY_URL, json.dumps(SAVED_DISCOVERY))
    assert warnings == []


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
def test_unusable_oidc_provider_raises(metadata, client_secret, expected):
    """Applies to a saved copy as much as to a live document."""
    with pytest.raises(OidcDiscoveryError, match=expected):
        oidc_identity_provider_args_from_discovery(
            metadata, DISCOVERY_URL, client_secret=client_secret
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
        load_oidc_discovery(DISCOVERY_URL, None)


@pytest.mark.usefixtures("unreachable_saml_endpoint", "warnings")
def test_saved_saml_metadata_deploys_while_the_endpoint_is_down():
    """A saved copy builds the IdP while the URL is down and keeps the descriptor URL.

    Keeping the URL in extraConfig means adding a copy doesn't change the live IdP.
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


@pytest.mark.usefixtures("unreachable_oidc_endpoint", "warnings")
def test_saved_oidc_discovery_deploys_while_the_endpoint_is_down():
    recording = RecordingMocks()
    pulumi.runtime.set_mocks(recording, preview=False)
    resource_options = pulumi.ResourceOptions()

    @pulumi.runtime.test
    def register():
        onboard_oidc_org(
            OIDCIdpConfig(
                idp_alias="partner",
                idp_display_name="Partner",
                org_oidc_metadata_url=DISCOVERY_URL,
                org_oidc_discovery_json=json.dumps(SAVED_DISCOVERY),
                first_login_flow=keycloak.authentication.Flow(
                    "first-login", realm_id="olapps", alias="first-login"
                ),
                realm_id="olapps",
                resource_options=resource_options,
                client_id="client",
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
    (idp,) = recording.of_type("keycloak:oidc/identityProvider:IdentityProvider")
    assert idp["authorizationUrl"] == "https://idp.example.com/auth"
    assert idp["tokenUrl"] == "https://idp.example.com/token"
    assert idp["issuer"] == "https://idp.example.com"


# The OIDC IdPs in olapps.py configured with a client secret rather than
# private_key_jwt.
CLIENT_SECRET_DISCOVERY_COPIES = {"google", "ubsu"}


@pytest.mark.parametrize(
    "path",
    sorted(olapps.SAVED_IDP_METADATA_DIR.glob("*_metadata.xml")),
    ids=lambda path: path.name,
)
def test_saved_saml_copy_builds_an_idp(path: Path):
    """A copy is only used during an outage, so check it can be used now."""
    fields = extract_saml_metadata(load_saml_metadata(None, path.read_text()))
    assert fields["entity_id"]
    assert fields["single_sign_on_service_url"]


@pytest.mark.parametrize(
    "path",
    sorted(olapps.SAVED_IDP_METADATA_DIR.glob("*_oidc_discovery.json")),
    ids=lambda path: path.name,
)
def test_saved_oidc_copy_builds_an_idp(path: Path):
    """A copy is only used during an outage, so check it can be used now."""
    name = path.name.removesuffix("_oidc_discovery.json")
    args = oidc_identity_provider_args_from_discovery(
        json.loads(path.read_text()),
        str(path),
        client_secret="a-secret"  # pragma: allowlist secret
        if name in CLIENT_SECRET_DISCOVERY_COPIES
        else None,
    )
    assert args["authorization_url"]
    assert args["token_url"]
