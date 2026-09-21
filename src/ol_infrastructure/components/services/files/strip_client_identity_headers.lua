-- Drop the identity headers the gateway mints, when they arrive from a client.
--
-- APISIX's openid-connect plugin attaches X-Userinfo, X-ID-Token and
-- X-Refresh-Token to a request once it has a verified session, and it clears
-- any inbound copy of them first -- but only on the routes it is attached to.
-- A route on the same host without the plugin forwards whatever the client
-- sent. mitol-apigateway's middleware authenticates off X-Userinfo
-- (MITOL_APIGATEWAY_USERINFO_HEADER_NAME, HTTP_X_USERINFO) and creates or
-- updates the matching Django user, so one unprotected route on an OIDC host
-- is enough to impersonate or mint any account.
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
return function(conf, ctx)
    local core = require("apisix.core")
    local headers = (conf.identity_header_strip or {}).headers

    if not headers then
        return
    end

    for i = 1, #headers do
        local name = headers[i]
        -- Only touch what is actually there: at this point in the request
        -- nothing but the client can have set these, so a hit is a forgery
        -- attempt and worth a log line. Names only -- an X-Userinfo body is
        -- base64 user data.
        if core.request.header(ctx, name) ~= nil then
            -- set_header with a nil value removes it, and keeps APISIX's own
            -- ctx.headers / ctx.var caches in step, which a bare
            -- ngx.req.clear_header would not.
            core.request.set_header(ctx, name, nil)
            core.log.warn("stripped client-supplied identity header ", name,
                          " host=", ctx.var.host, " uri=", ctx.var.uri)
        end
    end
end
