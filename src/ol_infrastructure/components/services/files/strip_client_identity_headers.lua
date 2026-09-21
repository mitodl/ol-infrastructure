-- Drop the identity headers the gateway mints, when they arrive from a client.
--
-- APISIX's openid-connect plugin attaches X-Userinfo, X-ID-Token,
-- X-Raw-ID-Token and X-Refresh-Token to a request once it has a verified
-- session, and it clears any inbound copy of them first -- but only on the
-- routes it is attached to. A route on the same host without the plugin
-- forwards whatever the client sent. mitol-apigateway's middleware
-- authenticates off X-Userinfo (MITOL_APIGATEWAY_USERINFO_HEADER_NAME,
-- HTTP_X_USERINFO) and creates or updates the matching Django user, so one
-- unprotected route on an OIDC host is enough to impersonate or mint any
-- account.
--
-- Attached to an ApisixGlobalRule rather than to routes, because the hole is
-- exactly the set of routes nobody remembered to attach something to. Global
-- rules run after route matching but before the matched route's own rewrite
-- and access phases (apisix/init.lua http_access_phase), so this clears the
-- client's copy before openid-connect (rewrite, priority 2599) sets the real
-- one, on every route, whether or not that route carries the plugin.
--
-- Configuration arrives on the plugin config under `identity_header_strip`,
-- the same mechanism `oidc_error_callback_recovery.lua` uses.
--
--   identity_header_strip.headers  list of header names to clear
--
-- The list is passed in rather than hardcoded here because it is not the whole
-- family: X-Access-Token is deliberately absent. Tika's route uses that exact
-- header name for a client-supplied shared secret, checked in the access phase
-- (applications/tika/__main__.py) -- i.e. after this runs -- and mit-learn
-- sends it on every extraction request (learning_resources/etl/utils.py). No
-- application treats it as an identity assertion, so clearing it would buy
-- nothing and break content extraction.
--
-- See `identity_header_strip_plugin` in ../apisix.py for the deployment
-- reasoning and t/strip_client_identity_headers.t for the behavioural tests.

-- Clear one exact header name, if the client sent it.
local function clear_header(core, ctx, name)
    -- Only touch what is actually there: at this point in the request nothing
    -- but the client can have set these, so a hit is a forgery attempt and
    -- worth a log line. Names only -- an X-Userinfo body is base64 user data.
    if core.request.header(ctx, name) == nil then
        return
    end

    -- set_header with a nil value removes it, and unlike a bare
    -- ngx.req.clear_header it also updates APISIX's cached ctx.headers table
    -- so a later plugin reading through core.request does not see the header
    -- this just removed. (It does not invalidate the matching ctx.var entry
    -- for a dashed name -- modify_header clears ctx.var["http_x-userinfo"]
    -- while ctx.var's own __index reads it as http_x_userinfo -- which is
    -- harmless here only because nothing reads that var before this runs.)
    core.request.set_header(ctx, name, nil)
    core.log.warn("stripped client-supplied identity header ", name,
                  " host=", ctx.var.host, " uri=", ctx.var.uri)
end

return function(conf, ctx)
    local core = require("apisix.core")
    local headers = (conf.identity_header_strip or {}).headers

    if not headers then
        return
    end

    for i = 1, #headers do
        local name = headers[i]
        clear_header(core, ctx, name)

        -- The underscore spelling is a second, distinct header as far as
        -- nginx is concerned, and it reaches us: APISIX ships
        -- `underscores_in_headers on` (conf/nginx.conf, from cli/config.lua)
        -- and we rely on that elsewhere -- learn-ai's canvas routes
        -- authenticate off a `canvas_token` header. Django then folds both
        -- spellings onto one META key ("HTTP_%s" % name.upper().replace("-",
        -- "_")), so `X_Userinfo` arrives at the middleware as HTTP_X_USERINFO
        -- exactly like `X-Userinfo` would.
        --
        -- ngx.req.get_headers() does not paper over the difference in the
        -- direction that would matter: its metatable falls back from an
        -- underscore in the *queried* key to a dash, never the reverse, so
        -- looking up "X-Userinfo" misses a header received as "x_userinfo".
        -- Each spelling has to be asked for by name. Upstream's own clear in
        -- openid-connect.lua has the same gap; this does not.
        local underscored = name:gsub("%-", "_")
        if underscored ~= name then
            clear_header(core, ctx, underscored)
        end
    end
end
