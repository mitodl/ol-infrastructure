"""Canonical-origin behaviour, against a real APISIX.

Every assertion here is something the stubbed unit tests cannot reach: that
APISIX accepts a ``canonical_https_redirect`` block that is not in
serverless-pre-function's schema, that ``ngx.var.host`` really does drop the
port OpenResty received rather than a stub simply agreeing with us, and that
``serverless-pre-function`` honours the array order these two functions depend
on.

See ``oidc_gateway_pre_function_plugin`` in
src/ol_infrastructure/components/services/apisix.py.
"""

from __future__ import annotations

import pytest
import urllib3

from tests.apisix_integration.conftest import requires_docker

pytestmark = [requires_docker, pytest.mark.integration]


def test_plain_http_is_upgraded_to_https(origin_request):
    """The production failure: without this, openid-connect builds an http://
    redirect_uri that Keycloak rejects, and APISIX answers with an OIDC session
    cookie over cleartext.
    """
    status, headers = origin_request(path="/origin/hub/login")

    assert status == 308
    assert headers["Location"] == "https://nb.learn.mit.edu/origin/hub/login"


def test_explicit_port_is_stripped_from_the_redirect_target(origin_request):
    """``Host: <host>:443`` reaches lua-resty-openidc as ngx.var.http_host and
    produces an authority Keycloak has no registration for.  Reproduced against
    production on 2026-08-19.
    """
    status, headers = origin_request(host="nb.learn.mit.edu:443")

    assert status == 308
    assert headers["Location"] == "https://nb.learn.mit.edu/origin/"


def test_query_string_is_preserved(origin_request):
    """request_uri, not uri.  Dropping the query would break exactly the OIDC
    callbacks this is meant to rescue.
    """
    _, headers = origin_request(path="/origin/login/.apisix/redirect?code=a&state=b")

    assert headers["Location"] == (
        "https://nb.learn.mit.edu/origin/login/.apisix/redirect?code=a&state=b"
    )


def test_upgrade_wins_over_error_recovery_on_the_same_plugin(origin_request):
    """Both functions are attached to this route and the request is plain HTTP,
    so the origin fix has to answer first.  Recovering here instead would send
    the browser back into an http:// login and fail again at Keycloak.
    """
    status, headers = origin_request(
        path="/origin/login/.apisix/redirect?error=temporarily_unavailable"
    )

    assert status == 308
    assert headers["Location"].startswith("https://")
    assert "apisix_oidc_recovery" not in headers.get("Set-Cookie", "")


def test_global_rule_upgrades_an_oidc_route_without_its_own_pre_function(
    origin_request,
):
    """The gap the global rule closes: a route carrying openid-connect and no
    per-application serverless-pre-function.  A real APISIX is what proves the
    global rule sees openid-connect on ``ctx.matched_route.value.plugins``.
    """
    status, headers = origin_request(path="/global-oidc/", host="pipelines.odl.mit.edu")

    assert status == 308
    assert headers["Location"] == "https://pipelines.odl.mit.edu/global-oidc/"
    assert "Set-Cookie" not in headers


def test_global_rule_leaves_a_route_without_openid_connect_alone(origin_request):
    """Proxied to the dead upstream rather than redirected."""
    status, _ = origin_request(path="/global-plain/", host="pipelines.odl.mit.edu")

    assert status == 502


def test_global_rule_still_strips_identity_headers_after_the_redirect_check(apisix):
    """Both functions share the one global serverless-pre-function, and
    serverless/init.lua stops at the first that returns a value.  If the
    canonical function ever returned one on fall-through, the strip would stop
    running cluster-wide with nothing else to notice.
    """
    response = urllib3.PoolManager(retries=False).request(
        "GET",
        f"{apisix}/global-strip-echo/",
        headers={"Host": "pipelines.odl.mit.edu", "X-Userinfo": "forged"},
        redirect=False,
        timeout=10.0,
    )

    assert response.status == 200
    assert response.data.decode().strip() == "nil"
