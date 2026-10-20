-- Restart the OIDC login flow when a callback is certain to fail, instead of
-- letting the openid-connect plugin serve a 500.
--
-- Attached as a serverless-pre-function in the `rewrite` phase, where this
-- plugin's priority (10000) outranks openid-connect's (2599), so it sees the
-- callback before lua-resty-openidc does.  Two shapes of callback are
-- recovered:
--
--   * the IdP redirected back with a recoverable `error` and no code, which
--     lua-resty-openidc treats as fatal;
--   * a `code` callback carrying none of the host's OIDC session cookies.
--     lua-resty-openidc validates `state` against the one stored in that
--     session, so with no session the check fails every time.  The browser
--     completing the login is not the one that started it (an email link
--     opened elsewhere, a fresh in-app webview), and a new flow in this
--     browser is the only way it can succeed.
--
-- Configuration arrives on the plugin config under `oidc_error_recovery`.
-- serverless/init.lua invokes each function as `func(conf, ctx)` and its schema
-- does not set additionalProperties, so extra keys validate and are readable
-- here -- no interpolation into the source is needed.
--
--   oidc_error_recovery.recoverable_errors   list of OAuth2 `error` codes to
--                                            restart the flow for
--   oidc_error_recovery.session_cookie_names OIDC session cookies any route on
--                                            this host may use; empty disables
--                                            the missing-session recovery
--   oidc_error_recovery.guard_cookie_name    loop-breaker cookie name
--   oidc_error_recovery.guard_max_age        guard cookie lifetime, seconds; also
--                                            the restart counter's TTL
--   oidc_error_recovery.restart_dict_name    lua_shared_dict counting restarts
--                                            per Keycloak session_state
--   oidc_error_recovery.max_restarts         restarts allowed per session_state
--                                            per guard window, per pod
--
-- See `oidc_gateway_pre_function_plugin` in ../apisix.py for why each branch
-- is here, and t/oidc_error_callback_recovery.t for the behavioural tests.
return function(conf, ctx)
    local uri = ngx.var.uri
    -- Attached to a host's shared plugin config, so this runs on every route on
    -- the host. APISIX's callback always sits at <login prefix>/.apisix/redirect,
    -- and the leading slash is part of the match: without it this also catches
    -- application paths that merely end in the same characters, such as
    -- /login/foo.apisix/redirect, and would redirect them.
    if not uri or not uri:match("/%.apisix/redirect$") then
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

    local reason
    if err then
        -- Only errors the IdP considers transient. access_denied means the user
        -- pressed Cancel, and invalid_request is a real misconfiguration:
        -- bouncing either back into /login would spin the browser against the
        -- IdP.
        for _, candidate in ipairs(opts.recoverable_errors or {}) do
            if candidate == err then
                reason = "error=" .. err
                break
            end
        end
    elseif args["code"] then
        -- Any one of the names being present means openid-connect may yet
        -- succeed, so it gets the request.  A host can carry more than one
        -- (mitxonline's shared config also serves /mitxonline/* on MIT Learn's
        -- host, under MIT Learn's cookie), and only a callback with none of
        -- them is certain to fail.
        local names = opts.session_cookie_names or {}
        if #names > 0 then
            reason = "no session cookie"
            for _, name in ipairs(names) do
                if ctx.var["cookie_" .. name] then
                    reason = nil
                    break
                end
            end
        end
    end
    if not reason then
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

    -- A browser that stores none of this host's cookies never sends the guard
    -- back, so the check above passes on every hop and it loops through
    -- Keycloak until the browser gives up.  Every hop of such a loop carries
    -- the same Keycloak `session_state`, so count restarts per value here as
    -- well.  The dict is per APISIX pod, which makes the bound max_restarts
    -- times the replica count rather than exact.  A cluster whose nginx config
    -- does not define the dict yet has ngx.shared[name] == nil and keeps the
    -- cookie-only behaviour, so this can ship ahead of the gateway change.
    local session_state = args["session_state"]
    if type(session_state) == "table" then
        session_state = session_state[1]
    end
    local restarts = opts.restart_dict_name and ngx.shared[opts.restart_dict_name]
    if session_state and restarts then
        -- Hashed so a client-chosen value cannot grow the key.  Scoped by host
        -- because the dict serves every host on the pod and the guard cookie
        -- it backs up is per host: one SSO session recovering on two hosts
        -- within the window must not spend the second host's restart.
        local count = restarts:incr(
            ngx.md5((ngx.var.host or "") .. "|" .. tostring(session_state)),
            1, 0, opts.guard_max_age
        )
        if count and count > opts.max_restarts then
            core.log.warn("oidc callback ", reason, " uri=", uri,
                          " restart limit reached, not restarting auth")
            return
        end
    end

    core.log.warn("oidc callback ", reason, " uri=", uri, " restarting auth")
    ngx.header["Set-Cookie"] = guard .. "=1; Path=/; Max-Age="
        .. tostring(opts.guard_max_age)
        .. "; Secure; HttpOnly; SameSite=Lax"

    -- The route serving the callback is by construction the unauth_action="auth"
    -- one, so its parent path re-enters the authorization flow that just failed.
    -- Deriving the target here is what lets one attachment cover every route
    -- group on a host (mit-learn serves both /login and /learn/login).
    --
    -- Note this strips `.apisix/redirect` and keeps the slash the match above
    -- required, so /login/.apisix/redirect yields /login/ rather than /login.
    return ngx.redirect((uri:gsub("%.apisix/redirect$", "")), 302)
end
