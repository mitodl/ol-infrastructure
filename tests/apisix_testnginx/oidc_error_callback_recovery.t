#
# APISIX test-nginx coverage for the OIDC error-callback recovery function.
#
# This layer runs the shipped Lua inside the same OpenResty build the gateway
# uses, so it catches anything that depends on the real ngx API rather than on
# our stubs: Lua pattern behaviour, ngx.header on a response ngx.redirect()
# generates, and nginx's own $cookie_<name> parsing.
#
# The container tests in tests/apisix_integration drive a full APISIX and cover
# the plugin-config path; this one isolates the function so a failure points at
# the Lua rather than at routing or schema validation.
#
use t::APISIX 'no_plan';

repeat_each(1);
no_long_string();
no_root_location();
log_level('info');

run_tests();

__DATA__

=== TEST 1: an expired authentication session is redirected back through login
--- config
    location /t {
        rewrite_by_lua_block {
            local recover = require("apisix.plugins.ol.oidc_error_callback_recovery")
            local conf = {
                oidc_error_recovery = {
                    recoverable_errors = {"temporarily_unavailable"},
                    guard_cookie_name = "apisix_oidc_recovery",
                    guard_max_age = 60,
                },
            }
            recover(conf, {var = {}})
        }
        content_by_lua_block {
            ngx.say("not reached")
        }
    }
--- request
GET /t/login/.apisix/redirect?error=temporarily_unavailable&state=x
--- error_code: 302
--- response_headers
Location: /t/login/
Set-Cookie: apisix_oidc_recovery=1; Path=/; Max-Age=60; Secure; HttpOnly; SameSite=Lax



=== TEST 2: a successful callback carrying a code is left alone
--- config
    location /t {
        rewrite_by_lua_block {
            local recover = require("apisix.plugins.ol.oidc_error_callback_recovery")
            local conf = {
                oidc_error_recovery = {
                    recoverable_errors = {"temporarily_unavailable"},
                    guard_cookie_name = "apisix_oidc_recovery",
                    guard_max_age = 60,
                },
            }
            recover(conf, {var = {}})
        }
        content_by_lua_block {
            ngx.say("passed through")
        }
    }
--- request
GET /t/login/.apisix/redirect?code=abc123&state=x
--- response_body
passed through



=== TEST 3: access_denied keeps failing rather than restarting the flow
--- config
    location /t {
        rewrite_by_lua_block {
            local recover = require("apisix.plugins.ol.oidc_error_callback_recovery")
            local conf = {
                oidc_error_recovery = {
                    recoverable_errors = {"temporarily_unavailable"},
                    guard_cookie_name = "apisix_oidc_recovery",
                    guard_max_age = 60,
                },
            }
            recover(conf, {var = {}})
        }
        content_by_lua_block {
            ngx.say("passed through")
        }
    }
--- request
GET /t/login/.apisix/redirect?error=access_denied&state=x
--- response_body
passed through



=== TEST 4: the guard cookie breaks the loop on a second failure
--- config
    location /t {
        rewrite_by_lua_block {
            local recover = require("apisix.plugins.ol.oidc_error_callback_recovery")
            local conf = {
                oidc_error_recovery = {
                    recoverable_errors = {"temporarily_unavailable"},
                    guard_cookie_name = "apisix_oidc_recovery",
                    guard_max_age = 60,
                },
            }
            -- nginx parses the cookie header; $cookie_<name> is what the
            -- function reads, so feed it through ctx.var the same way APISIX does.
            recover(conf, {var = {cookie_apisix_oidc_recovery = ngx.var.cookie_apisix_oidc_recovery}})
        }
        content_by_lua_block {
            ngx.say("passed through")
        }
    }
--- request
GET /t/login/.apisix/redirect?error=temporarily_unavailable&state=x
--- more_headers
Cookie: apisix_oidc_recovery=1
--- response_body
passed through



=== TEST 5: a non-callback URI on the same host is untouched
--- config
    location /t {
        rewrite_by_lua_block {
            local recover = require("apisix.plugins.ol.oidc_error_callback_recovery")
            local conf = {
                oidc_error_recovery = {
                    recoverable_errors = {"temporarily_unavailable"},
                    guard_cookie_name = "apisix_oidc_recovery",
                    guard_max_age = 60,
                },
            }
            recover(conf, {var = {}})
        }
        content_by_lua_block {
            ngx.say("passed through")
        }
    }
--- request
GET /t/login/elsewhere?error=temporarily_unavailable
--- response_body
passed through



=== TEST 6: a repeated error parameter arrives as a table
--- config
    location /t {
        rewrite_by_lua_block {
            local recover = require("apisix.plugins.ol.oidc_error_callback_recovery")
            local conf = {
                oidc_error_recovery = {
                    recoverable_errors = {"temporarily_unavailable"},
                    guard_cookie_name = "apisix_oidc_recovery",
                    guard_max_age = 60,
                },
            }
            recover(conf, {var = {}})
        }
        content_by_lua_block {
            ngx.say("not reached")
        }
    }
--- request
GET /t/login/.apisix/redirect?error=temporarily_unavailable&error=other&state=x
--- error_code: 302
--- response_headers
Location: /t/login/
