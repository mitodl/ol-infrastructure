# 0013. APISIX leaves bearer tokens to MITx Online and MIT Learn

**Status:** Accepted
**Date:** 2026-09-30
**Deciders:** tmacey
**Technical Story:** Rootly INC-10, ol-infrastructure#4810 (reverted),
`tk-apisix-eliminate-oidc-state-mismatch-failures-78-e1dfb4` (part 2)

## Context

APISIX fronts mitxonline.mit.edu and api.learn.mit.edu with the `openid-connect` plugin,
through shared plugin configs built by `OLApisixOIDCResources`. That plugin handles the
browser login flow and, optionally, bearer tokens.

The plugin only reads the `Authorization` header when one of `bearer_only`,
`introspection_endpoint`, `public_key` or `use_jwks` is set (`rewrite()` and `introspect()`
in `apisix/plugins/openid-connect.lua`, APISIX 3.18.0). None of them are set today, so a
bearer token passes through to Django untouched. With `use_jwks` on, every request carrying
`Authorization: Bearer ...` (or `X-Access-Token`) is verified as a JWT from the discovery
document's issuer, Keycloak. A token that fails verification gets a 401 before
`unauth_action` is consulted, so the check applies to `unauth_action: pass` routes too.
Other schemes (`Api-Key`, `Token`) are never inspected.

ol-infrastructure#4810 set `use_jwks` on both hosts on 2026-06-23 so the MIT Learn ETL could
call MITx Online with a Keycloak client-credentials token (mitodl/hq#11671). The next day
Open edX course login broke with a redirect loop (Rootly INC-10), and the change was
reverted (`a2a59af42`).

The loop came from Open edX's `ol_social_auth` backend (open-edx-plugins). It finishes every
learner login by calling MITx Online's `/api/v0/userinfo/` with `Bearer <token>`, where the
token was issued by MITx Online's own Django OAuth Toolkit provider, not Keycloak. The
gateway rejected it, and the LMS sent the learner back to log in.

An inventory of bearer traffic on both hosts (Production APISIX access log, 6h to
2026-09-30T18:40Z, cookieless non-browser requests; the auth scheme comes from each
caller's code because the access log does not record the header):

| Path | Caller | Credential | Requests / 6h |
|---|---|---|---|
| mitxonline `/api/v0/userinfo/` | Open edX login (`social-auth-4.9.1`) | Bearer, MITx Online DOT | 2,088 |
| mitxonline `/api/openedx_webhook/certificate/`, `/enrollment/` | `ol_openedx_events_handler` | Bearer, MITx Online DOT | 837 |
| mitxonline `/api/internal/courses/` | MIT Learn ETL | `Api-Key` | 880 |
| mitxonline `/oauth2/token/` | Open edX login | client credentials in the body | 1,620 |
| api.learn search, content files, webhooks | assorted scripts | none that Django reads | ~600 |

No view in mitxonline or mit-learn accepts a Keycloak-issued bearer token. MIT Learn's DRF
authentication is session-only, and MITx Online accepts its own DOT tokens
(`OAuth2Authentication`) and API keys (`HasAPIKey`). The ETL requirement behind #4810 was
later met with an API key (`learning_resources/etl/mitxonline.py` sends
`Authorization: Api-Key ...`), so nothing still needs gateway bearer validation on these
hosts.

### Options considered

1. Split by path: give the Open edX-facing paths their own route that skips bearer
   validation, and put `use_jwks` on everything else. This only holds while every
   non-Keycloak bearer client is on a known path, and a new Open edX integration on a new
   path would break the same way INC-10 did.
2. Split by token: a pre-function reads the token's `iss` claim and routes it to a check for
   that issuer. It handles any mix of issuers, but adds custom Lua to the login path, which
   already carries the OIDC recovery pre-function, to serve a consumer that does not exist.
3. Leave bearer tokens to the applications: the gateway handles browser sessions only on
   these hosts, and Django authenticates any bearer token with the classes each view
   declares.

## Decision

Option 3. The shared OIDC plugin configs for mitxonline and mit_learn set none of
`bearer_only`, `introspection_endpoint`, `public_key` or `use_jwks`.

If a Keycloak service account needs to call one of these hosts, give it a dedicated route
that matches only its paths and the `Authorization: Bearer` header. Set `bearer_only`,
`use_jwks` and an explicit `claim_validator.issuer.valid_issuers` on that route's plugin
config only, never on a host's shared config. `applications/opik/__main__.py` is the
working example of that shape. Accepting the token in Django (e.g. a DRF authentication
class that validates Keycloak JWTs for the views that need it) is the alternative when the
view already has one.

`tests/ol_infrastructure/applications/test_apisix_bearer_passthrough.py` fails if any of the
four options appears as a keyword argument or string literal anywhere under
`applications/mitxonline/` or `applications/mit_learn/`. Against #4810's versions of those
files it reports all four `oidc_use_jwks=True` lines.

## Consequences

### Positive

- The failure mode behind INC-10 is caught in CI instead of on the Open edX login path.
- No new gateway Lua or routing rules to maintain.

### Negative

- The gateway does no bearer-token checking on these hosts. Each view's DRF authentication
  classes are the only check, which is the case today.
- The source-level test cannot see options that arrive another way (e.g. a shared helper
  outside these two directories that sets them). A change to `OLApisixOIDCResources` that
  starts emitting one of them by default would need its own review.

### Neutral

- A future Keycloak service-account consumer needs a dedicated route (or a Django
  authentication class) rather than a one-line config change.
- Not covered by the inventory: learn-ai's calls to api.learn, and any client that sends a
  cookie and a bearer token together, because the access log records neither the header
  nor its scheme.

## References

- apache/apisix 3.18.0, `apisix/plugins/openid-connect.lua`: `get_bearer_access_token`,
  `introspect`, and the `rewrite` branch that calls it
- mitodl/open-edx-plugins: `ol_social_auth/backends.py` (`user_data`),
  `ol_openedx_events_handler/tasks.py` (`_post_webhook`)
- mitodl/mitxonline: `main/settings.py` (`REST_FRAMEWORK`), `openedx/views.py`,
  `courses/views/internal/`
- mitodl/mit-learn: `main/settings.py` (`REST_FRAMEWORK`),
  `learning_resources/etl/mitxonline.py`
- Rootly INC-10, "Too many redirects when logging into learn/mitxonline", 2026-06-24

---

**Review History:**

| Date | Reviewer | Decision | Notes |
|------|----------|----------|-------|
| 2026-09-30 | | | Initial draft |

**Last Updated:** 2026-09-30
