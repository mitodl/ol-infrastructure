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
stripped client-supplied identity header X-Userinfo



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



=== TEST 13: a header pushed past ngx.req.get_headers()'s 100-header cap is still removed
--- config
    underscores_in_headers on;
    location /t {
        rewrite_by_lua_block {
            local strip = require("apisix.plugins.ol.strip_client_identity_headers")
            strip({identity_header_strip = {headers = {"X-Userinfo", "X-ID-Token", "X-Raw-ID-Token", "X-Refresh-Token"}}},
                  {var = {host = "api.learn.mit.edu", uri = "/t"}})
        }
        content_by_lua_block {
            -- 0 means "no cap", so this sees the header even where the capped
            -- default read does not.
            ngx.say("x-userinfo: ", tostring(ngx.req.get_headers(0)["X-Userinfo"]))
            ngx.say("capped read would have seen it: ",
                    tostring(ngx.req.get_headers()["X-Userinfo"] ~= nil))
        }
    }
--- more_headers
X-Filler-001: 1
X-Filler-002: 2
X-Filler-003: 3
X-Filler-004: 4
X-Filler-005: 5
X-Filler-006: 6
X-Filler-007: 7
X-Filler-008: 8
X-Filler-009: 9
X-Filler-010: 10
X-Filler-011: 11
X-Filler-012: 12
X-Filler-013: 13
X-Filler-014: 14
X-Filler-015: 15
X-Filler-016: 16
X-Filler-017: 17
X-Filler-018: 18
X-Filler-019: 19
X-Filler-020: 20
X-Filler-021: 21
X-Filler-022: 22
X-Filler-023: 23
X-Filler-024: 24
X-Filler-025: 25
X-Filler-026: 26
X-Filler-027: 27
X-Filler-028: 28
X-Filler-029: 29
X-Filler-030: 30
X-Filler-031: 31
X-Filler-032: 32
X-Filler-033: 33
X-Filler-034: 34
X-Filler-035: 35
X-Filler-036: 36
X-Filler-037: 37
X-Filler-038: 38
X-Filler-039: 39
X-Filler-040: 40
X-Filler-041: 41
X-Filler-042: 42
X-Filler-043: 43
X-Filler-044: 44
X-Filler-045: 45
X-Filler-046: 46
X-Filler-047: 47
X-Filler-048: 48
X-Filler-049: 49
X-Filler-050: 50
X-Filler-051: 51
X-Filler-052: 52
X-Filler-053: 53
X-Filler-054: 54
X-Filler-055: 55
X-Filler-056: 56
X-Filler-057: 57
X-Filler-058: 58
X-Filler-059: 59
X-Filler-060: 60
X-Filler-061: 61
X-Filler-062: 62
X-Filler-063: 63
X-Filler-064: 64
X-Filler-065: 65
X-Filler-066: 66
X-Filler-067: 67
X-Filler-068: 68
X-Filler-069: 69
X-Filler-070: 70
X-Filler-071: 71
X-Filler-072: 72
X-Filler-073: 73
X-Filler-074: 74
X-Filler-075: 75
X-Filler-076: 76
X-Filler-077: 77
X-Filler-078: 78
X-Filler-079: 79
X-Filler-080: 80
X-Filler-081: 81
X-Filler-082: 82
X-Filler-083: 83
X-Filler-084: 84
X-Filler-085: 85
X-Filler-086: 86
X-Filler-087: 87
X-Filler-088: 88
X-Filler-089: 89
X-Filler-090: 90
X-Filler-091: 91
X-Filler-092: 92
X-Filler-093: 93
X-Filler-094: 94
X-Filler-095: 95
X-Filler-096: 96
X-Filler-097: 97
X-Filler-098: 98
X-Filler-099: 99
X-Filler-100: 100
X-Filler-101: 101
X-Filler-102: 102
X-Filler-103: 103
X-Filler-104: 104
X-Filler-105: 105
X-Filler-106: 106
X-Filler-107: 107
X-Filler-108: 108
X-Filler-109: 109
X-Filler-110: 110
X-Filler-111: 111
X-Filler-112: 112
X-Filler-113: 113
X-Filler-114: 114
X-Filler-115: 115
X-Filler-116: 116
X-Filler-117: 117
X-Filler-118: 118
X-Filler-119: 119
X-Filler-120: 120
X-Userinfo: eyJzdWIiOiAiZm9yZ2VkIn0=
--- request
GET /t
--- response_body
x-userinfo: nil
capped read would have seen it: false



=== TEST 14: a mixed dash/underscore spelling is removed too
--- config
    underscores_in_headers on;
    location /t {
        rewrite_by_lua_block {
            local strip = require("apisix.plugins.ol.strip_client_identity_headers")
            strip({identity_header_strip = {headers = {"X-Userinfo", "X-ID-Token", "X-Raw-ID-Token", "X-Refresh-Token"}}},
                  {var = {host = "api.learn.mit.edu", uri = "/t"}})
        }
        content_by_lua_block {
            local h = ngx.req.get_headers(0)
            ngx.say("mixed id: ", tostring(h["X_ID-Token"]))
            ngx.say("mixed raw: ", tostring(h["X-Raw_ID_Token"]))
        }
    }
--- more_headers
X_ID-Token: id-token
X-Raw_ID_Token: raw-id-token
--- request
GET /t
--- response_body
mixed id: nil
mixed raw: nil
