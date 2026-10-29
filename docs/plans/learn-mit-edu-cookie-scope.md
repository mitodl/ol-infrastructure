# Narrowing the .learn.mit.edu cookie set

Tasks: `tk-investigate-narrowing-the-learn-mit-edu-parent-s-4b6008` (this spec) and
`tk-stop-sending-7kb-of-unrelated-cookies-on-jupyter-c4be8d` (the original measurement),
under the SOA login flow epic `tk-soa-login-flow-holistic-fix-up-epic-6bb5a1`
(https://github.com/mitodl/hq/issues/12175).

Every host under `learn.mit.edu` receives the cookies that MITx Online's Open edX and MIT
Learn set at the parent domain. The task asked which of those can be narrowed, what
depends on the parent scope, and how to migrate each cookie without breaking logins.

The short answer: the MIT Learn gateway session has to stay at `.learn.mit.edu` by design,
the JupyterHub gateway session is already host-only, and the one sizeable set that can move
without logging anyone out is Open edX's JWT, user-info and language cookies (about 2.2 KB
per request). The Open edX session cookie can't move yet because learn-ai started reading
it from `api.learn.mit.edu` on 2026-10-29.

Each claim is marked _verified_ (with the file, line and commit, or the query it rests on)
or _open_. Line numbers are against ol-infrastructure `b621f3beb` unless stated.

## Current state

### Measurement

Loki, `grafanacloud-logs` (production), 24 hours ending 2026-10-29 15:25 UTC, on the
APISIX access log fields from `apisix_official.py:705-709`. Stream selector
`{namespace="operations", container="apisix", stream="stdout"} |= "host=nb.learn.mit.edu"`.

| Measure | Value |
| --- | --- |
| Requests to nb.learn.mit.edu | 508,291 |
| Mean / p50 / p95 `cookie_bytes` | 9,819 / 9,509 / 11,582 |
| Requests carrying `mitlearn_apisix_session` | 505,195 (99.4%) |
| Requests carrying `mitxonline-production-edx-lms-sessionid` | 507,939 (99.9%) |
| Requests carrying `mitxonline-production-edx-jwt-cookie-header-payload` | 235,959 (46%) |
| Mean `mitlearn_apisix_session` size | 3,519 |
| Mean `jupyterhub_apisix_session` size | 3,637 |
| Mean JWT header-payload cookie size | 725 |

Both OIDC session cookies have grown since the August sample (3,290 and 3,334 then). A
sample line from the same hour had both at 3,792-3,798 bytes, which is close to
lua-resty-session's 4,096-byte single-cookie limit (see the APISIX section).

Some browsers also send 60-90 `.mit.edu` analytics cookies (one sample: 14,724 bytes,
89 cookies). Those are set by other mit.edu properties and nothing in this spec touches
them.

### Inventory of what reaches nb.learn.mit.edu

Sizes are from one production request on 2026-10-29 (name plus value).

| Cookie | Bytes | Set by | Domain | Config |
| --- | --- | --- | --- | --- |
| `mitlearn_apisix_session` | 3,798 | APISIX, Learn session | `.learn.mit.edu` | `mit_learn/__main__.py:2211,2243` |
| `jupyterhub_apisix_session` | 3,792 | APISIX, JupyterHub | host-only nb | `jupyterhub/deployment.py:241-266` |
| `mitxonline-production-edx-user-info` | 1,071 | Open edX LMS | `.learn.mit.edu` | `SHARED_COOKIE_DOMAIN`, derived |
| `mitxonline-production-edx-jwt-cookie-header-payload` | 735 | Open edX LMS | `.learn.mit.edu` | same |
| `mitxonline-production-edx-jwt-cookie-signature` | 389 | Open edX LMS | `.learn.mit.edu` | same |
| `mitxonline-production-openedx-language-preference` | 52 | Open edX LMS | `.learn.mit.edu` | same |
| `edxloggedin` | 16 | Open edX LMS | `.learn.mit.edu` | same |
| `mitxonline-production-edx-lms-sessionid` | 227 | Open edX LMS | `.learn.mit.edu` | `edxapp/k8s_configmaps.py:237` |
| `session_mitlearn` | 249 | mit-learn Django | `.learn.mit.edu` | `mit_learn/Pulumi.Production.yaml:40` |
| `learn_csrftoken` | 48 | mit-learn Django | `.learn.mit.edu` | `mit_learn/__main__.py:1544` |
| `csrf_mitxonline` | 48 | mitxonline Django | `learn.mit.edu` | mitxonline `main/middleware.py:72-120` |
| `learn_ai_csrftoken` | 51 | learn-ai Django | not traced | |
| `_xsrf`, `jupyterhub-*` | ~550 | JupyterHub | host-only nb | JupyterHub's own |

The five Open edX cookies that follow `SHARED_COOKIE_DOMAIN` add up to about 2,260 bytes
when present.

## Does anything depend on the parent scope?

### Open edX: the parent scope was incidental

_Verified._ `SESSION_COOKIE_DOMAIN = f".{domains['lms'].split('.', 1)[-1]}"`
(`edxapp/k8s_configmaps.py:237`) takes the LMS hostname's parent. The expression predates
the move: it came over from the YAML template in `ae16db691` (2025-09-26) with the comment
"The session cookie domain appears to still be required for enabling the preview subdomain
to share authentication with LMS (TMM 2021-12-20)" (visible in the removed lines of
`b26965489`). That was written when the LMS was `courses.mitxonline.mit.edu`, so the
expression produced `.mitxonline.mit.edu`. #3496 (CI/QA, 2025-09-02) and #3565
(production, 2025-09-16) moved the LMS to `courses.learn.mit.edu` for branding. Neither PR
body mentions cookies. The expression has produced `.learn.mit.edu` since then without
anyone choosing it.

_Verified._ edx-platform derives the JWT and user-info cookie domain from the session
domain: `SHARED_COOKIE_DOMAIN = Derived(lambda settings: settings.SESSION_COOKIE_DOMAIN)`
(openedx/edx-platform master `7f0416c`, `openedx/envs/common.py:2431`), used by
`standard_cookie_settings` and `delete_logged_in_cookies`
(`openedx/core/djangoapps/user_authn/cookies.py:70-104`) and by the language cookie
(`openedx/core/djangoapps/lang_pref/helpers.py:23,35`). ol-infrastructure doesn't set
`SHARED_COOKIE_DOMAIN` anywhere in `src/` (git grep). MITx Online production runs
edx-platform master (`bridge/settings/openedx/version_matrix.py:33-39`).

The two settings the task pointed at don't involve the parent domain:

- _Verified._ `CROSS_DOMAIN_CSRF_COOKIE_DOMAIN` is `domains["lms"]`
  (`k8s_configmaps.py:151`), i.e. `courses.learn.mit.edu`. It's read by
  `openedx/core/djangoapps/cors_csrf/middleware.py:107-154` and only covers the LMS host
  and its subdomains.
- _Verified._ `BASE_COOKIE_DOMAIN` is also the LMS host (`k8s_configmaps.py:118`).
  edx-platform declares it (`openedx/envs/common.py:2526`) and nothing in edx-platform
  reads it (git grep on mitodl/edx-platform `70d9eb86dff`). _Open:_ edx-enterprise and our
  plugins weren't searched.
- _Verified._ The MFE build's `SESSION_COOKIE_DOMAIN` is the LMS host
  (`ol_concourse/pipelines/open_edx/mfe/pipeline.py:170`), not the parent. frontend-platform
  doesn't read it at all (openedx/frontend-platform main `3dd79d0`, grep of `src/`). The
  only reader in our MFE checkouts is frontend-app-learning removing a popup cookie
  (`src/alerts/logistration-alert/AccountActivationAlert.jsx:33`).

Hosts that legitimately need the Open edX cookies, all under `.courses.learn.mit.edu`:

- `courses.learn.mit.edu`: the LMS, the frontend-base Site Project served under `/apps`
  (`k8s_configmaps.py:240-264`), and the legacy MFEs.
- `preview.courses.learn.mit.edu`: shares the LMS session (the 2021 reason above).
- `studio.courses.learn.mit.edu`: the authoring MFE reads the LMS JWT and user-info cookies
  and calls `/login_refresh` cross-origin (explained in
  `local-dev/apps/openedx/configmaps/lms-config.yaml:121-137`).

### The one new dependency: learn-ai reads the edX session cookie

_Verified._ #6671 (merged 2026-10-29, `7489c17fe`) set
`LEARNER_API_OPENEDX_COOKIE_NAME: "mitxonline-production-edx-lms-sessionid"` for learn-ai
(`learn_ai/Pulumi.Production.yaml:49-50`). learn-ai (mitodl/learn-ai `d159cd0`) takes that
cookie from the chat request it receives on `api.learn.mit.edu` and replays it to
`courses.learn.mit.edu` to read the learner's due dates
(`ai_chatbots/learner_api.py:36-40,100-140`, `ai_chatbots/tools.py:965-975`). The browser
only sends that cookie to `api.learn.mit.edu` because its Domain is `.learn.mit.edu`.
Narrowing `SESSION_COOKIE_DOMAIN` turns that tool into "the due dates couldn't be checked"
for everyone.

learn-ai forwards only the session cookie, not the JWT cookies, so it doesn't constrain
`SHARED_COOKIE_DOMAIN`.

_Open:_ whether learn-ai#105 is deployed to production yet.

### MIT Learn's gateway session: parent scope is the design

_Verified._ `mitlearn_apisix_session` is scoped to `.learn.mit.edu` on purpose, and the
login-flow epic is extending that, not reducing it:

- mit-learn's routes, learn-ai's `/ai/*` and mitxonline's `/mitxonline/*` on
  `api.learn.mit.edu` all read it (`mitxonline/definition.py:476-488`,
  `learn_ai/__main__.py:1182-1201`).
- `analytics.learn.mit.edu` reads it; the comment at `mit_learn/__main__.py:2201-2211`
  gives that as the reason it was broadened.
- #6402 (open) creates this session from `courses.learn.mit.edu` at the start of the edX
  login and fails preview if the LMS host isn't under the Learn cookie domain.
- #6683 (open, task `tk-p2-ol-infrastructure-one-declared-learn-session--d12c19`) makes the
  domain a field of `LearnSession`, shared by all four stacks.
- P3 / decision D3 (`tk-p3-session-topology-one-gateway-session-for-lear-47813a`) proposes
  serving more of MITx Online under `*.learn.mit.edu` on this one session.

No cookie Domain covers `api.`, `analytics.` and `courses.learn.mit.edu` without also
covering `nb.learn.mit.edu`. This spec doesn't propose narrowing it. Its size is the lever
(see below).

### MIT Learn's Django session and the CSRF cookies

- `session_mitlearn` (249 bytes) is set by Django on `api.learn.mit.edu`. _Verified:_ the
  Next.js frontend never reads cookies server-side (no `next/headers` or cookie forwarding
  in `frontends/` on mitodl/mit-learn `1a954f13b`). It could be host-only. #6683 instead
  ties it to `LearnSession.cookie_domain`. The saving is 249 bytes, so this spec leaves it.
- `learn_csrftoken`, `csrf_mitxonline`, `learn_ai_csrftoken` are set by APIs on
  `api.learn.mit.edu` and read by JavaScript on `learn.mit.edu`
  (`frontends/main/src/common/client-utils.ts:51`). The cookie must carry a Domain
  attribute covering `learn.mit.edu`, and any Domain attribute also matches every
  subdomain (RFC 6265 §5.1.3). mitxonline sets `Domain=learn.mit.edu` from the request's
  Origin for that reason (`main/middleware.py:72-120` on mitodl/mitxonline `37591397`).
  These can't be narrowed, and they total about 150 bytes.

## Who reads the Open edX JWT cookies

| Reader | Host | How | Evidence |
| --- | --- | --- | --- |
| frontend-base Site Project | courses.learn.mit.edu/apps | `accessTokenCookieName` read in JS | `k8s_configmaps.py:262`, frontend-base `runtime/auth/AxiosJwtTokenService.js:39` |
| Legacy MFEs (frontend-platform) | courses.learn.mit.edu | `ACCESS_TOKEN_COOKIE_NAME` read in JS | `mfe/pipeline.py:129-130`, frontend-platform `src/auth/AxiosJwtTokenService.js:38` |
| Authoring MFE | studio.courses.learn.mit.edu | same, plus `/login_refresh` to the LMS | `local-dev/.../lms-config.yaml:121-137` |
| LMS / Studio backends | courses., studio. | edx-drf-extensions JWT cookie auth | edx-platform |

_Verified absent_ (git grep for `jwt-cookie`, `edx-user-info`, `lms-sessionid`,
`edxapp-csrftoken`, `openedx-language-preference` on each default branch): mit-learn,
mitxonline, ol-django, ol-analytics-api, smoot-design, mitxonline-theme, open-edx-plugins.
learn-ai matched only for the session cookie (above). frontend-component-header-mitol
matched only in `.env.development` and test setup, which are build-time cookie names for
an MFE served on the LMS host.

_Verified._ edx-notes-api on `notes.learn.mit.edu` doesn't use the LMS cookies: the LMS
hands the notes frontend a token (`lms/djangoapps/edxnotes/helpers.py:53`,
`lms/static/js/edxnotes/views/notes_factory.js:24-39`).

## The APISIX OIDC session cookies

### jupyterhub_apisix_session is host-only

_Verified._ The JupyterHub OIDC resource sets a cookie name and no cookie domain
(`jupyterhub/deployment.py:241-266`). The component only emits `session.cookie_domain`
when one is set (`components/services/apisix.py:806-809`), and lua-resty-session's default
domain is nil (lua-resty-session 4.1.5 `lib/resty/session.lua:2472`), which makes a
host-only cookie. It reaches nb.learn.mit.edu because it belongs to nb.learn.mit.edu. It
isn't sent to `authoring.nb.learn.mit.edu` or any other host. There is no scope problem
with this cookie, only a size problem.

### What the session cookie contains

Versions: chart 2.18.2 (`bridge/lib/versions.py:38`) ships APISIX 3.19.0 (chart
`appVersion`), whose rockspec pins lua-resty-openidc 1.9.0 and lua-resty-session 4.1.5
(`apisix-master-0.rockspec:50,55`). _Open:_ the running image tag wasn't read from the
cluster.

- Cookie format: an 82-byte header (type, flags, 32-byte session id, times, sizes, tag,
  MAC), base64url-encoded, followed by the encrypted payload
  (`lib/resty/session.lua:70-92`). One cookie holds at most 4,096 bytes. Beyond that the
  payload is split into `<name>2`, `<name>3` and so on, up to nine cookies
  (`session.lua:98-100,1091-1092`).
- Payload: `session_contents` defaults to `access_token`, `enc_id_token`, `id_token`, `user`
  all true (`components/services/apisix.py:696-701`), and no stack overrides it (git grep
  for `oidc_session_contents`). lua-resty-openidc stores the decoded ID token claims, the
  userinfo response, the raw ID token JWT, and the raw access token JWT plus the refresh
  token whenever `access_token` is stored (`lua-resty-openidc/lib/resty/openidc.lua:278-285,
  1695-1731`).
- Compression is already on. lua-resty-session deflates any payload over 1,024 bytes when
  that makes it smaller (`session.lua:957-972`, default at `session.lua:2495`). APISIX
  pins `lua-ffi-zlib` (`apisix-master-0.rockspec:86`). The plugin's session schema is
  `additionalProperties = false` (`apisix/plugins/openid-connect.lua:178-309`), so the
  threshold can't be tuned from route config. The `set $session_compressor zlib;` in
  `apisix_official.py:782` is a lua-resty-session 3.x variable that 4.x doesn't read.
- _Open:_ how the ~3.5 KB splits between the five stored values. Nothing measures it
  today. It can be measured in QA by decrypting one session with the client's session
  secret, or by logging `#cjson.encode(v)` per key from a temporary serverless function on
  a CI route.

JupyterHub's resource asks for `scope="openid email"` (`jupyterhub/deployment.py:261`) and
its cookie is the same size as Learn's, which asks for `organization:*`. The organization
claims are not what makes these cookies large.

### Which stored values each consumer needs

| Value | Upstream use | Who needs it |
| --- | --- | --- |
| `user` | `X-Userinfo` header (`openid-connect.lua:1497-1500`) | mit-learn, learn-ai, mitxonline via `mitol.apigateway` (`ol-django src/apigateway/mitol/apigateway/api.py:19-54`), ol-analytics-api (`core/auth/userinfo.py:21`) |
| `id_token` | `X-ID-Token` header (`openid-connect.lua:1491-1494`) | mitxonline reads `auth_time` from it (`authentication/backends/apisix_remote_user_org.py:19-80`) |
| `enc_id_token` | `id_token_hint` on RP-initiated logout (`openidc.lua:1860,1907-1908`) | every resource with a logout path |
| `access_token`, `refresh_token` | Renewal. When renewal fails, the session is marked unauthenticated on the next request (`openidc.lua:2091-2128`) | every resource that should notice a Keycloak-side session end before `absolute_timeout` |

nb.learn.mit.edu sets no `authenticator_class` (`jupyterhub/Pulumi.Production.yaml:4-16`),
so it gets the `tmp` authenticator (`lib/jupyterhub_config.py:58-80`), which reads no
header. On that host `user` and `id_token` have no consumer.

_Verified._ Dropping `id_token` doesn't break the authenticated check. The plugin passes
when the session has `authenticated` set, which is set at the code exchange regardless of
`session_contents` (`openidc.lua:1682,2093,2120-2123`).

_Open:_ how Keycloak handles RP-initiated logout with a `post_logout_redirect_uri` and no
`id_token_hint`. This spec keeps `enc_id_token` everywhere, so it doesn't depend on the
answer.

### Ways to make them smaller

Trim `session_contents` per resource. JupyterHub can drop `user` and `id_token`. The Learn
session can't drop anything without app changes, because every value has a consumer.
Existing cookies keep decrypting. A session only gets smaller when it's next written in
full, i.e. at the next login. The saving per value is _open_ until the composition is
measured.

Move session data to Redis (`session.storage = "redis"`, accepted by the schema at
`openid-connect.lua:239-243`). With a storage backend the cookie carries only the header
(`session.lua:1050-1058`): about 135 bytes with the name, against ~3.6 KB today. That
saves ~3.4 KB per cookie. Costs:

- A new stateful dependency on the request path of every OIDC route. While Redis is down,
  every gateway session reads as absent.
- There's no Redis for APISIX today (git grep of `apisix_official.py` and
  `components/services/apisix.py`). It would need an HA deployment and a password delivered
  through `secretRef` (the plugin already treats `session.redis.password` as a secret,
  `openid-connect.lua:644`).
- Switching a resource's storage abandons every existing session for it. I haven't tested
  this. It follows from cookie-stored payloads not being in Redis under their session id.
- The Learn session is shared by four stacks, so they have to switch in one apply. Once
  #6683 lands, that's one field on `LearnSession`.

A side effect, and a judgment call rather than a verified benefit: sid-keyed server-side
sessions are the first point at which deleting a gateway session from the server is
possible at all, which cookie storage can't do. That's relevant to the epic's logout work.

Move JupyterHub off `learn.mit.edu`. This would stop the Learn session, the Open edX
cookies and the Learn CSRF cookies reaching it (~6 KB). It changes the notebook URL that
course content links to, plus its Keycloak redirect URIs and certificates. That's a large
change to make for cookie bytes on a host whose traffic is mostly same-origin static and
API calls.

Strip `Cookie` upstream for static paths. Already considered and set aside in the sibling
task, because the client still sends the bytes.

## Migration plan per cookie

Two library behaviors drive the ordering below.

- _Verified._ When a browser holds two cookies with the same name and path (one at the old
  Domain, one at the new), both are sent. Django keeps the last one (`parse_cookie`,
  Django 5.2.17 `django/http/cookie.py:7-23`). The `cookie` 0.4.2 package behind
  frontend-platform's `universal-cookie` 4.0.4 keeps the first one ("only assign once",
  `index.js:69-70`, from frontend-app-learning's `node_modules`). lua-resty-session reads
  through nginx's `$cookie_<name>` (`session.lua:519`). _Open:_ which copy nginx returns
  for a duplicated name. I didn't check the nginx source.
- RFC 6265 §5.4 says browsers SHOULD order same-path cookies by creation time, oldest
  first. The stale parent copy is the older one, so it's the one JavaScript reads.
  _Open:_ whether Safari follows that ordering.

So narrowing a domain without deleting the parent copies leaves the backend and the MFE
reading different cookies. edX's own logout only deletes at the new domain
(`cookies.py:70-85`), so it won't clean the parent copies up.

### Open edX JWT, user-info, logged-in and language cookies

Target: `SHARED_COOKIE_DOMAIN = ".courses.learn.mit.edu"` for the mitxonline deployment
only (`.courses.rc.learn.mit.edu` and `.courses.ci.learn.mit.edu` in QA and CI). Leave
mitx, mitx-staging and xpro alone, because their parents (`.mitx.mit.edu`,
`.xpro.mit.edu`) have their own consumers that this spec didn't check.

What breaks: nothing found outside `*.courses.learn.mit.edu`.

Logout: no. These cookies are minted from the LMS session. When the MFE finds the JWT
cookie missing or expired, it calls `/login_refresh` and the LMS sets new ones at the new
domain.

Ordering:

1. Generalize `stale_session_cookie_cleanup_plugin` to take a list of cookie names (it
   takes one today, `components/services/apisix.py:89-150`). While there, fix its chunk
   pattern: it matches `<name>_N` (`apisix.py:144`), but lua-resty-session 4.x names chunks
   `<name>N` (`session.lua:1092`). learn-ai's `learner_api.py:100-115` already uses the
   correct form.
2. In one apply of the edxapp-mitxonline stack, set `SHARED_COOKIE_DOMAIN` explicitly and
   attach the cleanup plugin to the LMS, preview and Studio routes, with
   `cookie_domains=[".learn.mit.edu"]` and the five cookie names. The deletion fires
   whenever the request carries one of those names. It can't tell which copy it got, so it
   keeps re-deleting a parent copy that's already gone. That's a no-op for the browser and
   costs a few response headers.
3. QA first. In a browser that already has `.rc.learn.mit.edu` copies: load a courseware
   page, confirm DevTools shows only `.courses.rc.learn.mit.edu` copies afterwards, and
   check that `/login_refresh` fires once per page load, not in a loop. Repeat in Safari.
4. Production. Remove the plugin after the longest JWT cookie lifetime has passed. JWT
   cookie `max_age` is the session's expiry age (`cookies.py:88-104`). The session age is
   Django's default of two weeks (`SESSION_COOKIE_AGE` isn't set in
   `edxapp/config_builder.py` or `openedx/envs/common.py`; the default is
   `django/conf/global_settings.py:486`). Three weeks covers it.

_Open:_ whether the production settings loader passes an explicit `SHARED_COOKIE_DOMAIN`
through. lehrer's settings model declares the field (`lehrer
deployments/mit-ol/settings/lms/models/aqueduct.py:3824`), but I didn't trace which loader
production uses.

Rollback: revert the setting. That creates the same duplicate problem in reverse, so the
rollback has to evict the `.courses.learn.mit.edu` copies. Keep the plugin's domain list
parameterized so rollback is a config change.

Measure:

- Requests to nb.learn.mit.edu carrying `edx-jwt-cookie-header-payload:` (baseline 235,959
  of 508,291 over 24 hours) should fall toward zero over two weeks. Mean `cookie_bytes` on
  those requests should drop by about 2.2 KB.
- Watch the `/login_refresh` request rate on the LMS route (a loop shows up as a spike) and
  401s from MFE API calls in the first hour after the production apply.

### Open edX session cookie (lms-sessionid)

Target, when unblocked: `SESSION_COOKIE_DOMAIN = ".courses.learn.mit.edu"`. That still
covers preview and Studio. _Open:_ whether the CMS mounts the shared `60-interpolated-config`
ConfigMap (`k8s_configmaps.py:636-664`). No Studio session cookie reached nb.learn.mit.edu in
the 24-hour window.

What breaks: learn-ai's due-dates lookup (above). Nothing else found.

Logout: narrowing alone doesn't log anyone out. The server-side session is unchanged and
the parent copy keeps reaching the LMS. Django rewrites the cookie only when the session
changes (`SESSION_SAVE_EVERY_REQUEST: False`, `edxapp/config_builder.py:281`), so most
users keep only the parent copy until their next login or until it expires. After a login
both copies exist and Django reads the newer one. Deleting the parent copy at the gateway
without re-issuing it at the new domain logs out every MITx Online edX session at once.
That's the decision below.

Measure: the share of nb.learn.mit.edu requests carrying `edx-lms-sessionid:` (baseline
99.9%).

### mitlearn_apisix_session

Stays at `.learn.mit.edu`. Any size change goes through `LearnSession` once #6683 lands.
Measure with `loki_process_custom_apisix_oidc_session_cookie_bytes`, which tracks this cookie
only (`apisix_official.py:263-270`, `substructure/aws/eks/grafana.py:82-87`).

### jupyterhub_apisix_session

Stays host-only. Set `oidc_session_contents={"access_token": True, "enc_id_token": True}`
on both JupyterHub deployments.

- What breaks: `X-Userinfo` and `X-ID-Token` stop reaching JupyterHub, which reads neither.
- Logout: no. Existing cookies keep working and shrink at the next login, which is at most
  14 days away (`oidc_session_absolute_timeout`, `deployment.py:247`).
- Rollback: revert, with no user impact.
- Measure: the dashboard histogram doesn't cover this cookie, so use LogQL on
  `cookie_sizes` (the mean of `jupyterhub_apisix_session:` on nb.learn.mit.edu, 3,637
  bytes now).

### session_mitlearn and the CSRF cookies

No change (see above).

## Decisions

1. Narrow `SHARED_COOKIE_DOMAIN` for MITx Online's edX to `.courses.learn.mit.edu`, with
   gateway eviction of the parent copies. I recommend yes. It removes ~2.2 KB from every
   non-courseware `learn.mit.edu` request that carries them, with no logout.
2. Narrow the edX session cookie, and if so, force it by logging out every MITx Online edX
   session. I recommend not narrowing it now. learn-ai started depending on it today, and
   at 227 bytes it's the smallest of the edX cookies. If learn-ai stops forwarding it
   (e.g. by reading `global_id` from the edX JWT, hq#12830, which the D1-D7 comment
   already recommends exposing), migrate passively: change the domain and let parent
   copies age out over two weeks, with no logout. Force eviction only if a cutover date
   matters more than the logout.
3. Move gateway session data to Redis. I recommend not doing it for cookie bytes alone. It
   adds an availability dependency to every login. Revisit it under P2/P3, and sooner if
   the Learn session crosses 4,096 bytes and starts splitting into two cookies, which the
   current trend (3,290 in August, ~3,500 mean and 3,798 seen now) puts within reach.
4. Move JupyterHub off `learn.mit.edu`. I recommend no.

Trimming JupyterHub's `session_contents` doesn't need a decision.

## Open questions

- How the ~3.5 KB OIDC payload splits between `access_token`, `refresh_token`,
  `enc_id_token`, `id_token` and `user`.
- The running APISIX image version in production (inferred from the chart's `appVersion`).
- Whether learn-ai#105 is live in production, and whether `LEARNER_API_LEARN_COOKIE_NAME` is
  set anywhere (it isn't in ol-infrastructure).
- Whether production's edX settings loader honors an explicit `SHARED_COOKIE_DOMAIN`.
- Duplicate-cookie ordering in Safari, and how frontend-base's `universal-cookie` 8 handles
  duplicates (only frontend-platform's 4.0.4 was checked).
- Whether the CMS gets `SESSION_COOKIE_DOMAIN` from the shared interpolated config.
- Keycloak's behavior on RP-initiated logout without `id_token_hint`.
