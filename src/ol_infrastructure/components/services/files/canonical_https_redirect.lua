-- Send the request to the canonical https://<host> origin before the
-- openid-connect plugin builds a redirect_uri out of it.
--
-- Attached as a serverless-pre-function in the `rewrite` phase, where this
-- plugin's priority (10000) outranks openid-connect's (2599). That ordering is
-- the whole point: the shared-plugin defaults already carry APISIX's `redirect`
-- plugin with http_to_https, but `redirect` has a lower priority than
-- openid-connect, so on an OIDC route it never gets to run.
--
-- APISIX only derives a *relative* redirect_uri (`<uri>/.apisix/redirect`), and
-- lua-resty-openidc 1.8.0 -- the version pinned by APISIX 3.17 -- turns it
-- absolute from `ngx.var.scheme` and `ngx.var.http_host`. Both are raw
-- connection values, so a plain-HTTP request produces `http://...` and a
-- request carrying `Host: example.org:443` produces `https://example.org:443/...`.
-- Keycloak registers bare-host https URIs only, so it rejects both with
-- error="invalid_redirect_uri" and the login dies at the authorization endpoint,
-- before any callback exists to recover.
--
-- Normalising the request rather than the header is deliberate. lua-resty-openidc
-- does consult `Forwarded` / `X-Forwarded-Proto` / `X-Forwarded-Host` ahead of
-- those ngx vars, but on this deployment those headers demonstrably do not reach
-- it: sending any of the three against production changes nothing about the
-- redirect_uri. Setting them here would be a no-op that reads like a fix.
--
-- Configuration arrives on the plugin config under `canonical_https_redirect`,
-- the same mechanism `oidc_error_callback_recovery.lua` uses.
--
--   canonical_https_redirect.status  redirect status code
--
-- See `oidc_gateway_pre_function_plugin` in ../apisix.py for the deployment
-- reasoning and t/canonical_https_redirect.t for the behavioural tests.
return function(conf, ctx)
    -- $host is the Host header lowercased with any port stripped, falling back
    -- to server_name; $http_host is the header verbatim. Comparing them catches
    -- an explicit :443, an uppercased host, and anything else that would reach
    -- lua-resty-openidc as a non-canonical authority.
    local raw_host = ngx.var.http_host
    local host = ngx.var.host

    -- No Host header at all (HTTP/1.0). There is nothing to build a canonical
    -- origin from -- $host would be the server_name -- so leave it alone and let
    -- openid-connect's own 400 handle it.
    if not raw_host or not host then
        return
    end

    if ngx.var.scheme == "https" and raw_host == host then
        return
    end

    local core = require("apisix.core")
    local opts = conf.canonical_https_redirect or {}

    core.log.warn("non-canonical origin scheme=", ngx.var.scheme,
                  " host=", raw_host, " redirecting to https://", host)

    -- 308 rather than the 301 the shadowed `redirect` plugin would have sent:
    -- it preserves the method and body, so an upgraded POST stays a POST
    -- instead of being silently downgraded to a GET.
    return ngx.redirect("https://" .. host .. ngx.var.request_uri,
                        opts.status or 308)
end
