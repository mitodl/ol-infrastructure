"""The OIDC error-callback recovery plugin, against a real APISIX.

Every assertion here is something the stubbed unit tests cannot reach: that
APISIX accepts an ``oidc_error_recovery`` block that is not in
serverless-pre-function's schema and exposes it on ``conf``, and that OpenResty
emits the guard cookie on a redirect it generates itself.

A 302 means the plugin intercepted.  Any other status means it declined and the
request went on to the dead upstream, which in production is where the
openid-connect plugin takes over.

See ``oidc_gateway_pre_function_plugin`` in
src/ol_infrastructure/components/services/apisix.py.
"""

from __future__ import annotations

import pytest

from tests.apisix_integration.conftest import SESSION_COOKIE, requires_docker

pytestmark = [requires_docker, pytest.mark.integration]

EXPIRED_SESSION = (
    "?error=temporarily_unavailable&error_description=authentication_expired&state=x"
)
GUARD_COOKIE = "apisix_oidc_recovery"
HTTP_FOUND = 302


def test_expired_authentication_session_is_sent_back_through_login(callback):
    """The production case: 614 callbacks a day arrive shaped like this and are
    currently answered with a 21KB 500.
    """
    status, headers = callback(query=EXPIRED_SESSION)

    assert status == HTTP_FOUND
    assert headers["Location"] == "/login/"


def test_guard_cookie_survives_ngx_redirect(callback):
    """Set in the rewrite phase on a response ngx.redirect() generates.  A bot
    reviewer predicted OpenResty drops it, which would make the loop guard
    inert and risk an infinite redirect; this is the assertion that settles it
    on the real runtime rather than by reading lua-nginx-module's source.
    """
    _, headers = callback(query=EXPIRED_SESSION)

    assert headers["Set-Cookie"] == (
        f"{GUARD_COOKIE}=1; Path=/; Max-Age=60; Secure; HttpOnly; SameSite=Lax"
    )


def test_settings_reach_the_plugin_through_an_unschemad_config_key(callback):
    """``oidc_error_recovery`` is not part of serverless-pre-function's schema.
    It arrives because the CRD preserves unknown fields, the ingress controller
    holds plugin config as raw JSON, and APISIX's schema does not set
    additionalProperties.  Recovering at all proves the whole chain: the Lua
    reads its error list and cookie name off ``conf``, so if the block were
    dropped there would be nothing to recover.
    """
    status, headers = callback(query=EXPIRED_SESSION)

    assert status == HTTP_FOUND
    assert GUARD_COOKIE in headers["Set-Cookie"]


def test_redirect_target_follows_the_login_prefix(callback):
    """mit-learn serves two route groups on one host.  Deriving the target from
    the callback URI is what lets one attachment cover both.
    """
    _, headers = callback(path="/learn/login/.apisix/redirect", query=EXPIRED_SESSION)

    assert headers["Location"] == "/learn/login/"


def test_second_failure_falls_through_instead_of_looping(callback):
    """A persistently broken IdP has to surface as an error, not spin the
    browser between the gateway and Keycloak.
    """
    status, headers = callback(query=EXPIRED_SESSION, cookies={GUARD_COOKIE: "1"})

    assert status != HTTP_FOUND
    assert "Location" not in headers


def test_guard_matches_the_whole_cookie_name(callback):
    """Nginx parses the cookie header and exposes one variable per cookie, so a
    cookie whose name merely contains the guard's must not disable recovery.
    """
    status, _ = callback(
        query=EXPIRED_SESSION,
        cookies={f"not_{GUARD_COOKIE}": "1"},
    )

    assert status == HTTP_FOUND


def test_user_cancelling_login_is_not_recovered(callback):
    """access_denied means the user pressed Cancel."""
    status, _ = callback(query="?error=access_denied&state=x")

    assert status != HTTP_FOUND


def test_successful_callback_is_left_alone(callback):
    """A real login carries a code and the pre-auth session cookie, and must
    reach openid-connect untouched.
    """
    status, _ = callback(
        query="?code=abc123&state=x",
        cookies={SESSION_COOKIE: "pre-auth"},
    )

    assert status != HTTP_FOUND


def test_code_callback_without_a_session_is_sent_back_through_login(callback):
    """The larger share of what the error-callback fix left behind: a browser
    finishing a login it did not start holds no session to check `state`
    against, so openid-connect would 500 it every time.
    """
    status, headers = callback(
        query="?code=abc123&state=x",
        cookies={"unrelated": "1"},
    )

    assert status == HTTP_FOUND
    assert headers["Location"] == "/login/"
    assert GUARD_COOKIE in headers["Set-Cookie"]


def test_session_cookie_names_reach_the_plugin_through_the_config(callback):
    """The names travel in the same unschema'd block as the error list.  A
    request carrying the configured cookie passing through while one without
    it is redirected shows the list arrived intact.
    """
    with_session, _ = callback(
        query="?code=abc123&state=x", cookies={SESSION_COOKIE: "1"}
    )
    without_session, _ = callback(
        query="?code=abc123&state=x", cookies={f"not_{SESSION_COOKIE}": "1"}
    )

    assert (with_session != HTTP_FOUND, without_session) == (True, HTTP_FOUND)


def test_missing_session_recovery_respects_the_guard(callback):
    status, _ = callback(
        query="?code=abc123&state=x",
        cookies={GUARD_COOKIE: "1"},
    )

    assert status != HTTP_FOUND


def test_a_browser_storing_no_cookies_is_restarted_once_per_session_state(callback):
    """The loop seen in production: no guard cookie ever comes back, so only the
    shared-dict counter keyed on Keycloak's session_state can stop it.  This is
    also what proves APISIX created the dict from custom_lua_shared_dict.
    """
    query = "?code=abc123&state=x&session_state=no-cookie-loop"

    statuses = [callback(query=query)[0] for _ in range(3)]

    assert statuses[0] == HTTP_FOUND
    assert HTTP_FOUND not in statuses[1:]


def test_restart_counter_is_per_session_state(callback):
    """One looping browser must not use up another login's restart."""
    callback(query="?code=abc123&state=x&session_state=first-login")
    callback(query="?code=abc123&state=x&session_state=first-login")

    status, _ = callback(query="?code=abc123&state=x&session_state=second-login")

    assert status == HTTP_FOUND


def test_callback_without_an_error_parameter_is_left_alone(callback):
    status, _ = callback()

    assert status != HTTP_FOUND


def test_other_routes_on_the_host_are_left_alone(callback):
    """This hangs off a host's shared plugin config, so it runs in the rewrite
    phase of every route on the host, not just the callback.
    """
    status, _ = callback(
        path="/login/somewhere-else",
        query="?error=temporarily_unavailable",
    )

    assert status != HTTP_FOUND


def test_a_path_merely_ending_in_the_callback_suffix_is_left_alone(callback):
    """The suffix match includes the leading slash. Without it an application
    path such as /login/foo.apisix/redirect also matches, and — since this is
    attached host-wide — real requests would get redirected.
    """
    status, _ = callback(
        path="/login/foo.apisix/redirect",
        query=EXPIRED_SESSION,
    )

    assert status != HTTP_FOUND


def test_repeated_error_parameter_is_handled(callback):
    """?error=a&error=b makes get_uri_args return a table rather than a string;
    indexing it as a string would error out inside the rewrite phase.
    """
    status, headers = callback(
        query="?error=temporarily_unavailable&error=something_else&state=x",
    )

    assert status == HTTP_FOUND
    assert headers["Location"] == "/login/"
