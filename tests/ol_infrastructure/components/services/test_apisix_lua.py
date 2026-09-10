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
    oidc_gateway_pre_function_plugin,
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

function run(fn, conf, uri, args, ctx_vars)
    captured = {args = args, headers = {}, redirect = nil}
    ngx.var.uri = uri
    fn(conf, {var = ctx_vars})
    local redirect = captured.redirect
    return redirect and redirect.uri or nil,
           redirect and redirect.code or nil,
           captured.headers["Set-Cookie"]
end
"""


class Harness:
    """Runs the shipped Lua against a stubbed ngx/apisix.core.

    Both the function source and the ``conf`` table come from
    ``oidc_gateway_pre_function_plugin``, so these exercise the same plugin
    config the gateway is handed rather than a hand-built approximation.
    """

    # The builder fuses both pre-openid-connect functions into the one
    # serverless-pre-function APISIX allows per plugin config, so subclasses
    # select theirs by the conf block it reads.  Indexing by position would
    # silently test the wrong function if the order changed.
    FUNCTION_MARKER = "oidc_error_recovery"
    EXTRA_LUA = ""

    def __init__(self, **plugin_kwargs):
        self.lua = lupa.LuaRuntime(unpack_returned_tuples=True)
        # One chunk, not two: `captured` is a chunk-local upvalue that the
        # ngx.redirect stub closes over, so a runner defined in a separate
        # execute() would assign to a fresh global and capture nothing.
        self.lua.execute(LUA_STUBS + self.EXTRA_LUA)
        plugin_config = oidc_gateway_pre_function_plugin(**plugin_kwargs).config
        (source,) = [
            fn for fn in plugin_config["functions"] if self.FUNCTION_MARKER in fn
        ]
        self.fn = self.lua.execute(source)
        self.conf = self._to_lua(plugin_config)

    def _to_lua(self, value):
        """Deep-convert Python containers to Lua tables."""
        if isinstance(value, dict):
            return self.lua.table_from(
                {key: self._to_lua(item) for key, item in value.items()}
            )
        if isinstance(value, list):
            return self.lua.table_from([self._to_lua(item) for item in value])
        return value

    def callback(self, uri, args=None, cookies=None):
        """Return (redirect_uri, redirect_status, set_cookie) for one request.

        ``cookies`` is a name -> value mapping, exposed the way nginx exposes
        it: one ``ctx.var["cookie_<name>"]`` entry per cookie, parsed by nginx
        rather than by the plugin.
        """
        return self.lua.globals().run(
            self.fn,
            self.conf,
            uri,
            self._to_lua(args or {}),
            self._to_lua({f"cookie_{n}": v for n, v in (cookies or {}).items()}),
        )


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


def test_a_path_merely_ending_in_the_callback_suffix_is_untouched():
    """The suffix match includes the leading slash, so an application path that
    happens to end in the same characters is not treated as a callback.
    """
    uri, _, _ = Harness().callback(
        "/login/foo.apisix/redirect",
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
        cookies={"other": "1", "apisix_oidc_recovery": "1", "another": "2"},
    )

    assert uri is None


def test_guard_reads_the_exact_cookie_name():
    """The lookup is a single ctx.var["cookie_<name>"] read, so a cookie whose
    name merely contains the guard's must not disable recovery.
    """
    uri, _, _ = Harness().callback(
        "/login/.apisix/redirect",
        {"error": "temporarily_unavailable"},
        cookies={"not_apisix_oidc_recovery": "1"},
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


class OriginHarness(Harness):
    """Runs the canonical-origin function against a stubbed ngx.

    Unlike the recovery function this one reads only ``ngx.var`` -- scheme,
    host, http_host, request_uri -- so it gets its own runner rather than
    overloading ``run`` with parameters the other cases never use.
    """

    FUNCTION_MARKER = "canonical_https_redirect"
    EXTRA_LUA = """
    function run_origin(fn, conf, scheme, host, http_host, request_uri)
        captured = {args = {}, headers = {}, redirect = nil}
        ngx.var.scheme = scheme
        ngx.var.host = host
        ngx.var.http_host = http_host
        ngx.var.request_uri = request_uri
        fn(conf, {var = ngx.var})
        local redirect = captured.redirect
        return redirect and redirect.uri or nil, redirect and redirect.code or nil
    end
    """

    #: ``http_host`` defaults to mirroring ``host``; pass ``None`` explicitly for
    #: the HTTP/1.0 case where the request carries no Host header at all.
    MIRROR_HOST = object()

    def request(
        self, scheme="https", host="learn.mit.edu", http_host=MIRROR_HOST, uri="/"
    ):
        return self.lua.globals().run_origin(
            self.fn,
            self.conf,
            scheme,
            host,
            host if http_host is self.MIRROR_HOST else http_host,
            uri,
        )


def test_plain_http_is_upgraded_before_openid_connect_sees_it():
    """The measured failure: this is what sends Keycloak an http:// redirect_uri
    and answers with an OIDC session cookie over cleartext.
    """
    uri, status = OriginHarness().request(
        scheme="http", host="nb.learn.mit.edu", uri="/hub/login"
    )

    assert (uri, status) == ("https://nb.learn.mit.edu/hub/login", 308)


def test_explicit_port_in_host_is_stripped():
    """`Host: nb.learn.mit.edu:443` yields https://nb.learn.mit.edu:443/... which
    Keycloak rejects against its bare-host registration.  Reproduced live.
    """
    uri, status = OriginHarness().request(
        host="nb.learn.mit.edu", http_host="nb.learn.mit.edu:443"
    )

    assert (uri, status) == ("https://nb.learn.mit.edu/", 308)


def test_canonical_request_is_left_alone():
    """The overwhelming majority of traffic: must cost nothing and not redirect."""
    assert OriginHarness().request(uri="/search?q=x") == (None, None)


def test_query_string_survives_the_upgrade():
    """request_uri, not uri: dropping the query would break every OIDC callback
    that arrives over plain HTTP, which is precisely the traffic being fixed.
    """
    uri, _ = OriginHarness().request(
        scheme="http",
        host="api.learn.mit.edu",
        uri="/login/.apisix/redirect?code=a&state=b",
    )

    assert uri == "https://api.learn.mit.edu/login/.apisix/redirect?code=a&state=b"


def test_missing_host_header_is_left_to_openid_connect():
    """HTTP/1.0 with no Host: $host would be the server_name, so redirecting
    would invent an origin.  lua-resty-openidc already 400s this case.
    """
    assert OriginHarness().request(scheme="http", http_host=None) == (None, None)


def test_redirect_status_is_configurable():
    uri, status = OriginHarness(canonical_redirect_status=301).request(scheme="http")

    assert (uri, status) == ("https://learn.mit.edu/", 301)


def test_disabling_the_redirect_drops_the_function_entirely():
    """A host that must keep answering on plain HTTP turns this off without
    losing the error-callback recovery it shares a plugin with.
    """
    config = oidc_gateway_pre_function_plugin(canonical_https_redirect=False).config

    assert not [fn for fn in config["functions"] if "canonical_https_redirect" in fn]
    assert len(config["functions"]) == 1


def test_origin_normalisation_runs_before_error_recovery():
    """serverless/init.lua stops at the first function returning a code, so a
    plain-HTTP error callback must be upgraded rather than recovered -- the
    recovery redirect would otherwise send the browser back to an http:// login.
    """
    config = oidc_gateway_pre_function_plugin().config
    sources = config["functions"]

    assert "canonical_https_redirect" in sources[0]
    assert "oidc_error_recovery" in sources[1]
