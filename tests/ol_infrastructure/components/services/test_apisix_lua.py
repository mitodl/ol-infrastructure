"""Execute the Lua that apisix.py generates, rather than grepping its source.

The plugin helpers in ``components/services/apisix.py`` emit Lua as text, so
assertions on that text only prove a fragment is present -- an inverted branch
could satisfy every substring check and still break production authentication.
These tests run the generated function against a stubbed ``ngx``/``apisix.core``
and assert on what it actually does.

Worth the dependency: the ``%`` escaping in these f-string templates is easy to
get wrong in a way that is invisible in a substring assertion (``%%`` is not
collapsed by f-strings, only by printf-style formatting), and a wrong Lua
pattern silently matches nothing.
"""

from __future__ import annotations

import lupa

from ol_infrastructure.components.services.apisix import (
    oidc_error_callback_recovery_plugin,
)

# Stubs the generated function's whole world: the two ngx.var reads, the
# ngx.header write, ngx.redirect, and the apisix.core require.  `run` resets the
# captured state each call so cases cannot leak into one another.
LUA_STUBS = """
local captured = {}

package.loaded["apisix.core"] = {
    log = { warn = function() end },
    request = { get_uri_args = function() return captured.args end },
}

ngx = {
    var = {},
    header = setmetatable({}, {
        __newindex = function(_, key, value) captured.headers[key] = value end,
    }),
    redirect = function(uri, code)
        captured.redirect = {uri = uri, code = code}
    end,
}

function run(fn, uri, args, cookie)
    captured = {args = args, headers = {}, redirect = nil}
    ngx.var.uri = uri
    ngx.var.http_cookie = cookie
    fn({}, {})
    local redirect = captured.redirect
    return redirect and redirect.uri or nil,
           redirect and redirect.code or nil,
           captured.headers["Set-Cookie"]
end
"""


class Harness:
    """Loads one generated function into a fresh Lua runtime."""

    def __init__(self, **plugin_kwargs):
        self.lua = lupa.LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(LUA_STUBS)
        (source,) = oidc_error_callback_recovery_plugin(**plugin_kwargs).config[
            "functions"
        ]
        self.fn = self.lua.execute(source)

    def callback(self, uri, args=None, cookie=None):
        """Return (redirect_uri, redirect_status, set_cookie) for one request."""
        table = self.lua.table_from(
            {
                key: self.lua.table_from(value) if isinstance(value, list) else value
                for key, value in (args or {}).items()
            }
        )
        return self.lua.globals().run(self.fn, uri, table, cookie)


def test_expired_auth_session_is_sent_back_through_login():
    """The production case: 614 callbacks a day arrive shaped like this."""
    uri, status, _ = Harness().callback(
        "/login/.apisix/redirect",
        {"error": "temporarily_unavailable"},
    )

    assert (uri, status) == ("/login/", 302)


def test_recovery_sets_the_loop_guard_cookie():
    _, _, set_cookie = Harness().callback(
        "/login/.apisix/redirect",
        {"error": "temporarily_unavailable"},
    )

    assert set_cookie == (
        "apisix_oidc_recovery=1; Path=/; Max-Age=60; Secure; HttpOnly; SameSite=Lax"
    )


def test_redirect_target_follows_the_login_prefix():
    """mit-learn serves two route groups on one host, at /login and /learn/login.
    Deriving the target from the URI is what lets one attachment cover both.
    """
    uri, _, _ = Harness().callback(
        "/learn/login/.apisix/redirect",
        {"error": "temporarily_unavailable"},
    )

    assert uri == "/learn/login/"


def test_successful_callback_passes_through_to_openid_connect():
    """A real login carries a code; this plugin must not touch it."""
    uri, _, set_cookie = Harness().callback(
        "/login/.apisix/redirect",
        {"code": "abc123", "state": "xyz"},
    )

    assert uri is None
    assert set_cookie is None


def test_user_cancelling_login_is_not_recovered():
    """access_denied means the user pressed Cancel. Bouncing them back into
    /login would spin the browser between the gateway and Keycloak.
    """
    uri, _, _ = Harness().callback(
        "/login/.apisix/redirect",
        {"error": "access_denied"},
    )

    assert uri is None


def test_non_callback_routes_are_untouched():
    """This hangs off a host's shared plugin config, so it sees every request
    on the host, not just the callback.
    """
    uri, _, _ = Harness().callback(
        "/api/v1/courses",
        {"error": "temporarily_unavailable"},
    )

    assert uri is None


def test_callback_without_an_error_parameter_is_untouched():
    uri, _, _ = Harness().callback("/login/.apisix/redirect", {})

    assert uri is None


def test_guard_cookie_stops_a_second_recovery():
    """A persistently broken IdP has to surface as an error rather than an
    infinite redirect, so recovery happens at most once per guard window.
    """
    uri, _, _ = Harness().callback(
        "/login/.apisix/redirect",
        {"error": "temporarily_unavailable"},
        cookie="other=1; apisix_oidc_recovery=1; another=2",
    )

    assert uri is None


def test_guard_matches_the_whole_cookie_name():
    """Substring matching here would let an unrelated cookie disable recovery."""
    uri, _, _ = Harness().callback(
        "/login/.apisix/redirect",
        {"error": "temporarily_unavailable"},
        cookie="not_apisix_oidc_recovery=1",
    )

    assert uri == "/login/"


def test_repeated_error_parameter_is_handled():
    """?error=a&error=b makes get_uri_args return a table, not a string."""
    uri, _, _ = Harness().callback(
        "/login/.apisix/redirect",
        {"error": ["temporarily_unavailable", "something_else"]},
    )

    assert uri == "/login/"


def test_an_empty_recoverable_list_disables_recovery():
    """The no-op configuration has to actually recover nothing."""
    uri, _, _ = Harness(recoverable_errors=[]).callback(
        "/login/.apisix/redirect",
        {"error": "temporarily_unavailable"},
    )

    assert uri is None


def test_a_custom_error_list_is_honoured_at_runtime():
    harness = Harness(recoverable_errors=["server_error"])

    assert (
        harness.callback("/login/.apisix/redirect", {"error": "server_error"})[0]
        == "/login/"
    )
    assert (
        harness.callback(
            "/login/.apisix/redirect", {"error": "temporarily_unavailable"}
        )[0]
        is None
    )


def test_custom_guard_lifetime_reaches_the_cookie():
    _, _, set_cookie = Harness(guard_max_age=90).callback(
        "/login/.apisix/redirect",
        {"error": "temporarily_unavailable"},
    )

    assert "Max-Age=90;" in set_cookie
