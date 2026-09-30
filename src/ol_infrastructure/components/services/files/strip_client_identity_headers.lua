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
-- Two things a header-stripping function has to get right, both of which an
-- earlier revision of this file got wrong and both of which have a regression
-- test (TEST 13 and TEST 14):
--
-- * NEVER gate the removal on having read the header first.
--   ngx.req.get_headers() stops at 100 headers by default (lua-nginx's
--   NGX_HTTP_LUA_MAX_HEADERS; APISIX's core.request passes no limit), and
--   nginx forwards header 101 upstream all the same. A client that sends a
--   hundred junk headers before X-Userinfo would walk straight through a
--   check that asks "is it there?" first. ngx.req.set_header(name, nil)
--   operates on nginx's real header list, so clearing unconditionally has no
--   such blind spot -- and on a request without the header it is a no-op.
--
-- * Clear EVERY separator spelling, not just the all-dash one. nginx runs
--   with underscores_in_headers on (APISIX's own default, and learn-ai's
--   canvas routes depend on it for `canvas_token`), so X_ID-Token is a
--   distinct header that reaches the upstream, and Django folds every
--   spelling onto one key: "HTTP_%s" % name.upper().replace("-", "_"). A
--   four-word name like X-Raw-ID-Token therefore has eight spellings that all
--   arrive as HTTP_X_RAW_ID_TOKEN. Upstream's own clear in openid-connect.lua
--   names one spelling each; this expands them.
--
-- See `identity_header_strip_plugin` in ../apisix.py for the deployment
-- reasoning and t/strip_client_identity_headers.t for the behavioural tests.

-- Memoised across requests: the configured names are a fixed short list, and
-- the chunk is loaded once per plugin-config version, so this expands each
-- name at most once rather than rebuilding the same tables per request.
local variants_by_name = {}

-- Every dash/underscore spelling of one header name, including the original.
local function separator_variants(name)
    local cached = variants_by_name[name]
    if cached then
        return cached
    end

    local variants = {name}
    local position = 1
    while true do
        local dash = name:find("-", position, true)
        if not dash then
            break
        end
        -- Substitution preserves length, so `dash` indexes the same separator
        -- in every variant built so far. Each one forks in two.
        local count = #variants
        for i = 1, count do
            local variant = variants[i]
            variants[count + i] =
                variant:sub(1, dash - 1) .. "_" .. variant:sub(dash + 1)
        end
        position = dash + 1
    end

    variants_by_name[name] = variants
    return variants
end

return function(conf, ctx)
    local core = require("apisix.core")
    local headers = (conf.identity_header_strip or {}).headers

    if not headers then
        return
    end

    for i = 1, #headers do
        local name = headers[i]
        local variants = separator_variants(name)

        -- Detection, for the log line only -- the removal below does not
        -- depend on it. $http_* is the right primitive for this: nginx
        -- resolves it by walking the real header list, so it has neither of
        -- the blind spots described at the top of this file. It is not capped
        -- at 100, and it matches any spelling, because nginx lowercases each
        -- header's name and maps '-' to '_' before comparing. One read covers
        -- the whole variant set. Read before the clears, since it is cached
        -- per request once resolved.
        --
        -- Nothing but the client can have set these at this point in the
        -- request, so a hit is a forgery attempt and worth recording. Names
        -- only -- an X-Userinfo body is base64 user data.
        local present = ngx.var["http_" .. variants[#variants]:lower()]

        for j = 1, #variants do
            -- Unconditional, and via set_header rather than a bare
            -- ngx.req.clear_header so APISIX's cached ctx.headers is kept in
            -- step for whatever reads it later. (It does not invalidate the
            -- matching ctx.var entry for a dashed name -- modify_header
            -- clears ctx.var["http_x-userinfo"] while ctx.var's own __index
            -- reads it as http_x_userinfo -- which is harmless here only
            -- because nothing reads that var before this runs.)
            core.request.set_header(ctx, variants[j], nil)
        end

        if present ~= nil then
            core.log.warn("stripped client-supplied identity header ", name,
                          " host=", ctx.var.host, " uri=", ctx.var.uri)
        end
    end
end
