#
# APISIX test-nginx coverage for the client identity-header strip.
#
# Running this inside the real OpenResty build is what makes it worth having:
# the function leans on ngx.req.get_headers()'s case-insensitive lookup and on
# core.request.set_header's nil-means-remove behaviour, and a stub would simply
# agree with whatever we assumed about both.  The assertions read the headers
# back through ngx.req.get_headers() in a later phase rather than through the
# function's own view of them, so a strip that only updated APISIX's cache
# without touching the request would fail here.
#
use t::APISIX 'no_plan';

repeat_each(1);
no_long_string();
no_root_location();
log_level('info');

run_tests();

__DATA__

=== TEST 1: a client-supplied X-Userinfo is removed
--- config
    underscores_in_headers on;
    location /t {
        rewrite_by_lua_block {
            local strip = require("apisix.plugins.ol.strip_client_identity_headers")
            strip({identity_header_strip = {headers = {"X-Userinfo", "X-ID-Token", "X-Refresh-Token"}}},
                  {var = {host = "api.learn.mit.edu", uri = "/t"}})
        }
        content_by_lua_block {
            ngx.say("x-userinfo: ", tostring(ngx.req.get_headers()["X-Userinfo"]))
        }
    }
--- more_headers
X-Userinfo: eyJzdWIiOiAiZm9yZ2VkIn0=
--- request
GET /t
--- response_body
x-userinfo: nil
--- error_log
stripped client-supplied identity header X-Userinfo



=== TEST 2: the match is case-insensitive, as nginx header lookup is
--- config
    underscores_in_headers on;
    location /t {
        rewrite_by_lua_block {
            local strip = require("apisix.plugins.ol.strip_client_identity_headers")
            strip({identity_header_strip = {headers = {"X-Userinfo"}}},
                  {var = {host = "api.learn.mit.edu", uri = "/t"}})
        }
        content_by_lua_block {
            ngx.say("x-userinfo: ", tostring(ngx.req.get_headers()["x-userinfo"]))
        }
    }
--- more_headers
x-uSeRiNfO: eyJzdWIiOiAiZm9yZ2VkIn0=
--- request
GET /t
--- response_body
x-userinfo: nil



=== TEST 3: every header in the list goes, not just the first
--- config
    underscores_in_headers on;
    location /t {
        rewrite_by_lua_block {
            local strip = require("apisix.plugins.ol.strip_client_identity_headers")
            strip({identity_header_strip = {headers = {"X-Userinfo", "X-ID-Token", "X-Refresh-Token"}}},
                  {var = {host = "api.learn.mit.edu", uri = "/t"}})
        }
        content_by_lua_block {
            local h = ngx.req.get_headers()
            ngx.say("id: ", tostring(h["X-ID-Token"]))
            ngx.say("refresh: ", tostring(h["X-Refresh-Token"]))
        }
    }
--- more_headers
X-ID-Token: id-token
X-Refresh-Token: refresh-token
--- request
GET /t
--- response_body
id: nil
refresh: nil



=== TEST 4: a header outside the list is left alone
--- config
    underscores_in_headers on;
    location /t {
        rewrite_by_lua_block {
            local strip = require("apisix.plugins.ol.strip_client_identity_headers")
            strip({identity_header_strip = {headers = {"X-Userinfo", "X-ID-Token", "X-Refresh-Token"}}},
                  {var = {host = "tika.example.org", uri = "/t"}})
        }
        content_by_lua_block {
            local h = ngx.req.get_headers()
            ngx.say("access: ", tostring(h["X-Access-Token"]))
            ngx.say("authorization: ", tostring(h["Authorization"]))
        }
    }
--- more_headers
X-Access-Token: tika-shared-secret
Authorization: Bearer some-api-token
--- request
GET /t
--- response_body
access: tika-shared-secret
authorization: Bearer some-api-token



=== TEST 5: an absent header is not logged and not invented
--- config
    underscores_in_headers on;
    location /t {
        rewrite_by_lua_block {
            local strip = require("apisix.plugins.ol.strip_client_identity_headers")
            strip({identity_header_strip = {headers = {"X-Userinfo"}}},
                  {var = {host = "api.learn.mit.edu", uri = "/t"}})
        }
        content_by_lua_block {
            ngx.say("x-userinfo: ", tostring(ngx.req.get_headers()["X-Userinfo"]))
        }
    }
--- request
GET /t
--- response_body
x-userinfo: nil
--- no_error_log
stripped client-supplied identity header



=== TEST 6: no configuration block is a no-op, not a crash
--- config
    underscores_in_headers on;
    location /t {
        rewrite_by_lua_block {
            local strip = require("apisix.plugins.ol.strip_client_identity_headers")
            strip({}, {var = {host = "api.learn.mit.edu", uri = "/t"}})
        }
        content_by_lua_block {
            ngx.say("x-userinfo: ", tostring(ngx.req.get_headers()["X-Userinfo"]))
        }
    }
--- more_headers
X-Userinfo: eyJzdWIiOiAiZm9yZ2VkIn0=
--- request
GET /t
--- response_body
x-userinfo: eyJzdWIiOiAiZm9yZ2VkIn0=



=== TEST 7: the underscore spelling of a listed header is removed too
--- config
    underscores_in_headers on;
    location /t {
        rewrite_by_lua_block {
            local strip = require("apisix.plugins.ol.strip_client_identity_headers")
            strip({identity_header_strip = {headers = {"X-Userinfo", "X-ID-Token", "X-Raw-ID-Token", "X-Refresh-Token"}}},
                  {var = {host = "api.learn.mit.edu", uri = "/t"}})
        }
        content_by_lua_block {
            local h = ngx.req.get_headers()
            ngx.say("underscored: ", tostring(h["x_userinfo"]))
            ngx.say("dashed: ", tostring(h["X-Userinfo"]))
        }
    }
--- more_headers
X_Userinfo: eyJzdWIiOiAiZm9yZ2VkIn0=
--- request
GET /t
--- response_body
underscored: nil
dashed: nil
--- error_log
stripped client-supplied identity header X_Userinfo



=== TEST 8: nginx really does deliver the underscore spelling
--- config
    underscores_in_headers on;
    location /t {
        content_by_lua_block {
            ngx.say("underscored: ", tostring(ngx.req.get_headers()["x_userinfo"]))
            ngx.say("dashed lookup: ", tostring(ngx.req.get_headers()["X-Userinfo"]))
        }
    }
--- more_headers
X_Userinfo: eyJzdWIiOiAiZm9yZ2VkIn0=
--- request
GET /t
--- response_body
underscored: eyJzdWIiOiAiZm9yZ2VkIn0=
dashed lookup: nil



=== TEST 9: X-Raw-ID-Token is in the family and goes with the rest
--- config
    underscores_in_headers on;
    location /t {
        rewrite_by_lua_block {
            local strip = require("apisix.plugins.ol.strip_client_identity_headers")
            strip({identity_header_strip = {headers = {"X-Userinfo", "X-ID-Token", "X-Raw-ID-Token", "X-Refresh-Token"}}},
                  {var = {host = "api.learn.mit.edu", uri = "/t"}})
        }
        content_by_lua_block {
            ngx.say("raw: ", tostring(ngx.req.get_headers()["X-Raw-ID-Token"]))
        }
    }
--- more_headers
X-Raw-ID-Token: raw-id-token
--- request
GET /t
--- response_body
raw: nil



=== TEST 10: a repeated header is removed in full, not just its first value
--- config
    underscores_in_headers on;
    location /t {
        rewrite_by_lua_block {
            local strip = require("apisix.plugins.ol.strip_client_identity_headers")
            strip({identity_header_strip = {headers = {"X-Userinfo"}}},
                  {var = {host = "api.learn.mit.edu", uri = "/t"}})
        }
        content_by_lua_block {
            local value = ngx.req.get_headers()["X-Userinfo"]
            ngx.say("type: ", type(value))
            ngx.say("x-userinfo: ", tostring(value))
        }
    }
--- more_headers
X-Userinfo: first
X-Userinfo: second
--- request
GET /t
--- response_body
type: nil
x-userinfo: nil



=== TEST 11: an empty header list is a no-op, not an error
--- config
    underscores_in_headers on;
    location /t {
        rewrite_by_lua_block {
            local strip = require("apisix.plugins.ol.strip_client_identity_headers")
            strip({identity_header_strip = {headers = {}}},
                  {var = {host = "api.learn.mit.edu", uri = "/t"}})
        }
        content_by_lua_block {
            ngx.say("x-userinfo: ", tostring(ngx.req.get_headers()["X-Userinfo"]))
        }
    }
--- more_headers
X-Userinfo: eyJzdWIiOiAiZm9yZ2VkIn0=
--- request
GET /t
--- response_body
x-userinfo: eyJzdWIiOiAiZm9yZ2VkIn0=



=== TEST 12: a config block with no header list is a no-op, not a crash
--- config
    underscores_in_headers on;
    location /t {
        rewrite_by_lua_block {
            local strip = require("apisix.plugins.ol.strip_client_identity_headers")
            strip({identity_header_strip = {}},
                  {var = {host = "api.learn.mit.edu", uri = "/t"}})
        }
        content_by_lua_block {
            ngx.say("x-userinfo: ", tostring(ngx.req.get_headers()["X-Userinfo"]))
        }
    }
--- more_headers
X-Userinfo: eyJzdWIiOiAiZm9yZ2VkIn0=
--- request
GET /t
--- response_body
x-userinfo: eyJzdWIiOiAiZm9yZ2VkIn0=
