-- Restart the OIDC login flow when the IdP redirects back with a recoverable
-- error, instead of letting the openid-connect plugin serve a 500.
--
-- Attached as a serverless-pre-function in the `rewrite` phase, where this
-- plugin's priority (10000) outranks openid-connect's (2599), so it sees the
-- callback before lua-resty-openidc treats the `error` parameter as fatal.
--
-- Configuration arrives on the plugin config under `oidc_error_recovery`.
-- serverless/init.lua invokes each function as `func(conf, ctx)` and its schema
-- does not set additionalProperties, so extra keys validate and are readable
-- here -- no interpolation into the source is needed.
--
--   oidc_error_recovery.recoverable_errors  list of OAuth2 `error` codes to
--                                           restart the flow for
--   oidc_error_recovery.guard_cookie_name   loop-breaker cookie name
--   oidc_error_recovery.guard_max_age       guard cookie lifetime, seconds
--
-- See `oidc_error_callback_recovery_plugin` in ../apisix.py for why each branch
-- is here, and t/oidc_error_callback_recovery.t for the behavioural tests.
return function(conf, ctx)
    local uri = ngx.var.uri
    -- Attached to a host's shared plugin config, so this runs on every route on
    -- the host. APISIX's callback always sits at <login prefix>/.apisix/redirect.
    if not uri or not uri:match("%.apisix/redirect$") then
        return
    end

    local core = require("apisix.core")
    local opts = conf.oidc_error_recovery or {}

    local args = core.request.get_uri_args(ctx)
    if not args then
        return
    end

    -- A repeated ?error=&error= yields a table rather than a string.
    local err = args["error"]
    if type(err) == "table" then
        err = err[1]
    end
    if not err then
        return
    end

    -- Only errors the IdP considers transient. access_denied means the user
    -- pressed Cancel, and invalid_request is a real misconfiguration: bouncing
    -- either back into /login would spin the browser against the IdP.
    local recoverable = false
    for _, candidate in ipairs(opts.recoverable_errors or {}) do
        if candidate == err then
            recoverable = true
            break
        end
    end
    if not recoverable then
        return
    end

    -- One recovery per browser per guard window. A persistently broken IdP has
    -- to surface as an error rather than an infinite redirect. nginx parses the
    -- cookie header itself and exposes one variable per cookie, so this is a
    -- single exact-name lookup.
    local guard = opts.guard_cookie_name
    if not guard or ctx.var["cookie_" .. guard] then
        return
    end

    core.log.warn("oidc callback error=", err, " uri=", uri, " restarting auth")
    ngx.header["Set-Cookie"] = guard .. "=1; Path=/; Max-Age="
        .. tostring(opts.guard_max_age)
        .. "; Secure; HttpOnly; SameSite=Lax"

    -- The route serving the callback is by construction the unauth_action="auth"
    -- one, so its parent path re-enters the authorization flow that just failed.
    -- Deriving the target here is what lets one attachment cover every route
    -- group on a host (mit-learn serves both /login and /learn/login).
    return ngx.redirect((uri:gsub("%.apisix/redirect$", "")), 302)
end
