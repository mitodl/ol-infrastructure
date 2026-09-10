#
# APISIX test-nginx coverage for the canonical-origin redirect function.
#
# This layer runs the shipped Lua inside the same OpenResty build the gateway
# uses, which is the only place the central assumption can actually be checked:
# that $host is the Host header lowercased with the port stripped while
# $http_host is the header verbatim.  A stub can be made to agree with us about
# that; nginx cannot.
#
# The scheme is always http here, so the "already canonical, do nothing" case
# is covered by the unit and container layers instead -- test-nginx gives us no
# way to present an https connection to the function.
#
use t::APISIX 'no_plan';

repeat_each(1);
no_long_string();
no_root_location();
log_level('info');

run_tests();

__DATA__

=== TEST 1: a plain-HTTP request is upgraded to the https origin
--- config
    location /t {
        rewrite_by_lua_block {
            local canonical = require("apisix.plugins.ol.canonical_https_redirect")
            canonical({canonical_https_redirect = {status = 308}}, {var = {}})
        }
        content_by_lua_block {
            ngx.say("not reached")
        }
    }
--- more_headers
Host: nb.learn.mit.edu
--- request
GET /t/hub/login
--- error_code: 308
--- response_headers
Location: https://nb.learn.mit.edu/t/hub/login



=== TEST 2: an explicit :443 in the Host header is stripped from the target
--- config
    location /t {
        rewrite_by_lua_block {
            local canonical = require("apisix.plugins.ol.canonical_https_redirect")
            canonical({canonical_https_redirect = {status = 308}}, {var = {}})
        }
        content_by_lua_block {
            ngx.say("not reached")
        }
    }
--- more_headers
Host: nb.learn.mit.edu:443
--- request
GET /t/
--- error_code: 308
--- response_headers
Location: https://nb.learn.mit.edu/t/



=== TEST 3: an uppercased host is canonicalised to lower case
--- config
    location /t {
        rewrite_by_lua_block {
            local canonical = require("apisix.plugins.ol.canonical_https_redirect")
            canonical({canonical_https_redirect = {status = 308}}, {var = {}})
        }
        content_by_lua_block {
            ngx.say("not reached")
        }
    }
--- more_headers
Host: NB.Learn.MIT.edu
--- request
GET /t/
--- error_code: 308
--- response_headers
Location: https://nb.learn.mit.edu/t/



=== TEST 4: the query string survives the upgrade
--- config
    location /t {
        rewrite_by_lua_block {
            local canonical = require("apisix.plugins.ol.canonical_https_redirect")
            canonical({canonical_https_redirect = {status = 308}}, {var = {}})
        }
        content_by_lua_block {
            ngx.say("not reached")
        }
    }
--- more_headers
Host: api.learn.mit.edu
--- request
GET /t/login/.apisix/redirect?code=abc&state=xyz
--- error_code: 308
--- response_headers
Location: https://api.learn.mit.edu/t/login/.apisix/redirect?code=abc&state=xyz



=== TEST 5: the status code is taken from the plugin config
--- config
    location /t {
        rewrite_by_lua_block {
            local canonical = require("apisix.plugins.ol.canonical_https_redirect")
            canonical({canonical_https_redirect = {status = 301}}, {var = {}})
        }
        content_by_lua_block {
            ngx.say("not reached")
        }
    }
--- more_headers
Host: nb.learn.mit.edu
--- request
GET /t/
--- error_code: 301
--- response_headers
Location: https://nb.learn.mit.edu/t/



=== TEST 6: an absent config block falls back to 308
--- config
    location /t {
        rewrite_by_lua_block {
            local canonical = require("apisix.plugins.ol.canonical_https_redirect")
            canonical({}, {var = {}})
        }
        content_by_lua_block {
            ngx.say("not reached")
        }
    }
--- more_headers
Host: nb.learn.mit.edu
--- request
GET /t/
--- error_code: 308
--- response_headers
Location: https://nb.learn.mit.edu/t/
