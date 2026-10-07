"""PKCE on the authorization-code flow, against a real APISIX.

``OLApisixOIDCResources`` emits ``use_pkce`` so that the Keycloak clients behind
the gateway can require PKCE.  Keycloak's ``pkce-enforcer`` checks both halves:
``code_challenge`` (S256) on the authorization request and a matching
``code_verifier`` on the token request.  A unit test can only show the option
is in the dict.  This runs the login against the pinned APISIX, with the IdP
stubbed by two routes on the same gateway, and checks what the plugin sends.

The stub token endpoint always answers ``invalid_grant``: the assertion is on
the request APISIX made, and a real token response would need a signed ID
token.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pulumi
import pytest
import urllib3

# Python 3.14+ compatibility
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())


class K8sMocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        return [args.name + "_id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


pulumi.runtime.set_mocks(K8sMocks())

from ol_infrastructure.components.services.apisix import (  # noqa: E402
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
)
from tests.apisix_integration.conftest import (  # noqa: E402
    TEST_SESSION_SECRET,
    requires_docker,
    run_apisix,
)

pytestmark = [requires_docker, pytest.mark.integration]

# APISIX reaches the stub IdP on its own listener, inside the container.
STUB_IDP = "http://127.0.0.1:9080/idp"
RECORDED_REQUEST_FILE = "/tmp/ol-pkce-token-request"  # noqa: S108
DISCOVERY_DOCUMENT = (
    '{"issuer":"https://sso.invalid/realms/test",'
    '"authorization_endpoint":"https://sso.invalid/realms/test/auth",'
    f'"token_endpoint":"{STUB_IDP}/token",'
    f'"jwks_uri":"{STUB_IDP}/certs",'
    '"id_token_signing_alg_values_supported":["RS256"]}'
)


def _serverless(body: str) -> dict[str, Any]:
    return {
        "serverless-pre-function": {
            "phase": "rewrite",
            "functions": [f"return function() {body} end"],
        }
    }


@pytest.fixture(scope="module")
def pkce_apisix(tmp_path_factory):
    """Run APISIX with one route configured by the real OIDC component.

    Separate from the shared ``apisix`` fixture: that one carries the global
    rule that upgrades plain-HTTP requests on OIDC routes before
    openid-connect runs, and this listener is plain HTTP.
    """
    oidc = OLApisixOIDCResources(
        "pkce-integration",
        oidc_config=OLApisixOIDCConfig(
            application_name="pkce-integration",
            k8s_namespace="pkce-integration",
            vault_path="sso/pkce-integration",
            vaultauth="pkce-integration",
        ),
    )
    openid_connect = {
        **oidc.get_full_oidc_plugin_config(unauth_action="auth")["config"],
        # The values the ingress controller merges in from the secretRef.
        "client_id": "ol-integration-test",
        "client_secret": "not-a-secret",  # pragma: allowlist secret
        "discovery": f"{STUB_IDP}/.well-known/openid-configuration",
    }
    openid_connect.setdefault("session", {})["secret"] = TEST_SESSION_SECRET
    dead_upstream = {"type": "roundrobin", "nodes": {"127.0.0.1:1": 1}}
    routes = {
        "routes": [
            {
                "id": "pkce-app",
                "uri": "/app/*",
                "upstream": dead_upstream,
                "plugins": {"openid-connect": openid_connect},
            },
            {
                "id": "stub-idp-discovery",
                "uri": "/idp/.well-known/openid-configuration",
                "upstream": dead_upstream,
                "plugins": _serverless(
                    "ngx.header['Content-Type'] = 'application/json' "
                    f"ngx.say('{DISCOVERY_DOCUMENT}') ngx.exit(200)"
                ),
            },
            {
                "id": "stub-idp-token",
                "uri": "/idp/token",
                "upstream": dead_upstream,
                "plugins": _serverless(
                    "ngx.req.read_body() "
                    f"local f = assert(io.open('{RECORDED_REQUEST_FILE}', 'w')) "
                    "f:write(ngx.req.get_body_data() or '') f:close() "
                    "ngx.status = 400 "
                    "ngx.header['Content-Type'] = 'application/json' "
                    'ngx.say(\'{"error":"invalid_grant"}\') ngx.exit(400)'
                ),
            },
            {
                "id": "stub-idp-last-token-request",
                "uri": "/idp/last-token-request",
                "upstream": dead_upstream,
                "plugins": _serverless(
                    f"local f = io.open('{RECORDED_REQUEST_FILE}') "
                    "ngx.say(f and f:read('*a') or '') ngx.exit(200)"
                ),
            },
        ]
    }
    with run_apisix(
        tmp_path_factory.mktemp("apisix-pkce-conf"),
        routes,
        container_name="ol-apisix-integration-test-pkce",
    ) as base_url:
        yield base_url


def test_login_sends_s256_challenge_and_matching_verifier(pkce_apisix):
    http = urllib3.PoolManager(retries=False)

    login = http.request("GET", f"{pkce_apisix}/app/page", redirect=False, timeout=10.0)

    assert login.status == 302
    authorization = parse_qs(urlsplit(login.headers["Location"]).query)
    assert authorization["code_challenge_method"] == ["S256"]
    (challenge,) = authorization["code_challenge"]

    cookies = "; ".join(
        cookie.split(";", 1)[0] for cookie in login.headers.getlist("Set-Cookie")
    )
    (redirect_uri,) = authorization["redirect_uri"]
    (state,) = authorization["state"]
    http.request(
        "GET",
        f"{pkce_apisix}{urlsplit(redirect_uri).path}?code=stub-code&state={state}",
        headers={"Cookie": cookies},
        redirect=False,
        timeout=10.0,
    )

    recorded = http.request(
        "GET", f"{pkce_apisix}/idp/last-token-request", timeout=10.0
    )
    token_request = parse_qs(recorded.data.decode().strip())
    assert token_request["grant_type"] == ["authorization_code"]
    (verifier,) = token_request["code_verifier"]
    assert (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
        == challenge
    )
