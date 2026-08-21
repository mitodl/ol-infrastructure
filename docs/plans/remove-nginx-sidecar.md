# Removing the Nginx sidecar in favor of APISix → Granian direct

Seven Django deployments on EKS run an `nginx:1.31.3` sidecar in front of Granian,
wired up by `OLApplicationK8sConfig.import_nginx_config` in
`src/ol_infrastructure/components/services/k8s.py`. The sidecar listens on
`DEFAULT_NGINX_PORT` (8071), proxies to Granian on `DEFAULT_WSGI_PORT` (8073),
and is what the Service and every APISix route actually point at.

edxapp LMS/Studio already run without it
(`src/ol_infrastructure/applications/edxapp/k8s_resources.py`,
`import_nginx_config=False` + `granian_config.static_path_mounts`), which is the
precedent this plan generalizes.

## What the sidecar actually does, per app

(Struck-through rows no longer have a sidecar.)

| | static from | `/media` | `hash.txt` | X-Forwarded-Proto | body cap | other |
|---|---|---|---|---|---|---|
| ~~odl_video_service~~ | *nothing* | — | — | passthrough | 500M | 5 YouTube redirects |
| ~~ocw_studio~~ | `/src/staticfiles` | — | yes | passthrough | 25M | |
| ~~xpro~~ | `/src/staticfiles` | — | yes | passthrough | 25M | |
| ~~micromasters~~ | `$uri`, `/src/staticfiles` | — | yes | passthrough | 25M | |
| ~~learn_ai~~ | `$uri`, `/src/staticfiles` | — | — | *unset* | — | `proxy_buffering off` |
| ~~mitxonline~~ | `$uri`, `/src/staticfiles` | — | yes | **`$scheme`** | 25M | |
| ~~mit_learn~~ | `$uri`, `/src/staticfiles` | `/src/django_media` | — | **`$scheme`** | 25M | gzip on JSON |

Common to all but odl_video_service: `expires max` and
`add_header Access-Control-Allow-Origin *` on `/static/*`.
Common to all six with a static block: `location = /.well-known/dnt-policy.txt
{ return 204; }`. Common to all but mit_learn/mitxonline/learn_ai: a
`/nginx-health` endpoint that nothing probes (the probes use the apps' own
`/health/*` paths).

`large_client_header_buffers 4 32k`/`64k` is already set globally at the APISix
gateway (`apisix_official.py`, `configurationSnippet.httpStart` sets
`4 64k`), so those lines are redundant today.

### The two `X-Forwarded-Proto` variants matter

Five configs do the right thing — `set $my_scheme $http_x_forwarded_proto`,
falling back to `$scheme` — i.e. they re-forward what APISix already set.
**mit_learn and mitxonline instead set `X-Forwarded-Proto $scheme`**, and since
nginx listens on plain HTTP inside the pod, `$scheme` is always `http`. Those two
are actively downgrading APISix's `https` to `http`.

This turns out to be inert: neither app sets `SECURE_PROXY_SSL_HEADER`, and both
have `SECURE_SSL_REDIRECT` forced off from the stack config
(`MITOL_SECURE_SSL_REDIRECT` / `MITX_ONLINE_SECURE_SSL_REDIRECT` = `"False"`), so
Django never consults the header. Removing the sidecar lets the real `https`
through for the first time — a no-op for these two, but it is the kind of change
that only stays a no-op while `SECURE_PROXY_SSL_HEADER` stays unset. Worth a
re-check at rollout time rather than an assumption.

micromasters is the one app that *does* set
`SECURE_PROXY_SSL_HEADER=HTTP_X_FORWARDED_PROTO,https`, and its config is
already in the passthrough form, so it is unaffected.

### The duplicate CORS header is real

`OLApisixSharedPlugins` attaches a default `cors` plugin
(`allow_origins: "**"`, `allow_credential: True`) to every route using a shared
plugin config, and mit_learn's `/*` catch-all is one of them. So a `/static/*`
response today carries both APISix's echoed `Access-Control-Allow-Origin:
<origin>` and nginx's `Access-Control-Allow-Origin: *`. After removal only the
APISix header remains, which is the correct one — `*` cannot legally be paired
with credentials anyway.

### Body-size caps get *looser*, not tighter

APISix ships `client_max_body_size: 0` (unlimited) and `apisix_official.py` does
not override it, so today's effective cap is the sidecar's 25M/500M. Removing the
sidecar removes the cap. Any app that relies on it needs an explicit
`client-control` plugin on its route; none appear to.

## Component gaps in `GranianConfig`

`static_path_mounts` alone does not cover what the six static-serving apps need.
Granian's CLI (verified against `granian/cli.py` at v2.7.4, the version in the
ocw-studio image) exposes `--static-path-mount` (repeatable),
`--static-path-route` (repeatable, default `/static`) and
`--static-path-expires` (default `86400`). Concretely:

1. ~~**No `static_path_routes`.**~~ **Closed.** mit_learn serves `/media/*` from
   `/src/django_media`; that needed a second route/mount pair.
   `GranianConfig.static_path_routes` now emits one `--static-path-route` per
   entry, paired positionally with `static_path_mounts` (mirrors Granian's own
   `_init_static_mounts`, which requires equal lengths and refuses to start
   otherwise -- see stage 5).
2. ~~**No `static_path_expires`.**~~ **Closed.** `GranianConfig.static_path_expires`
   emits the flag; `STATIC_ASSET_MAX_AGE_SECONDS` (315360000) in
   `bridge/lib/magic_numbers.py` is what nginx's `expires max` resolves to.
   Granian's static handler emits `Cache-Control: max-age=<n>` and nothing else
   (`src/files.rs`), so this reproduces the sidecar's directive but not its
   legacy `Expires` header.
3. **No per-file override.** `/static/hash.txt` (`expires -1`,
   `Cache-Control: private`) is not expressible in Granian. It needs an APISix
   route with `response-rewrite`, on ocw_studio, xpro, micromasters and
   mitxonline. *By design — this stays at the gateway.*
4. **No CORS header on Granian-served static.** Confirmed: Granian sets no CORS
   header at all. Apps with a shared plugin config get one from APISix; the rest
   need an explicit `/static/*` route. Verify per app rather than assume.
5. ~~**Two-directory fallback unverified.**~~ **Closed — no fallthrough, and it
   doesn't matter.** Five configs do `try_files $uri $uri/ /staticfiles/$1`,
   i.e. `/src/<uri>` *then* `/src/staticfiles/<uri>`. Read granian v2.7.4's
   source directly: `_init_static_mounts` (`granian/server/common.py`)
   requires one `--static-path-route` per `--static-path-mount`, so multiple
   mounts on one shared route isn't even configurable, and
   `match_static_file` (`src/files.rs`) returns on the first matching
   mount's `NotFound` rather than trying the next one, so there is no
   miss-and-fall-through primitive to reach for even with distinct routes.
   Moot for every app that gates on it: the fallback's first tier only ever
   mattered before `collectstatic` ran, and every K8s deployment here runs it
   via an init container before the app container starts. See stage 4 below.

`/.well-known/dnt-policy.txt → 204` needs one APISix route per app. Kept rather
than dropped: it costs one `mocking` plugin and keeps a crawled path off the
Granian blocking-thread pool, where Django would answer it with a 404.

### The named-port trap in `OLApisixHTTPRoute`

Gateway API `backendRef.port` must be numeric, so
`OLApisixHTTPRoute._resolve_backend_port` maps the port *name* `"http"` to a
hardcoded `8071` — `DEFAULT_NGINX_PORT`. Apps on the Gateway API path
(ocw_studio, xpro, learn_ai, and any other caller passing
`application_lb_service_port_name`) therefore keep routing to the sidecar's port
after the sidecar is gone, and 502 on every request, with nothing in the Pulumi
diff to hint at it. `OLApplicationK8s` now publishes the resolved number as
`application_lb_service_port`; every route on a sidecar-free app must use it.
This does not affect the `ApisixRoute` CRD path (odl_video_service), which takes
the number directly.

### Probe ports follow the application port

`OLApplicationK8sConfig.probe_configs` defaulted to a literal dict pinned to
`DEFAULT_NGINX_PORT`, so every app dropping its sidecar had to restate all three
probes just to change a port. It now defaults to `None` and the component builds
them from the resolved application port via `default_probe_configs()`. Verified
no-op: a `pulumi preview` of learn_ai (sidecar still on, default probes) shows no
Deployment diff. The six apps that pass `probe_configs` explicitly are unchanged
and still own the port they name.

## Proof of concept: odl_video_service

Done — see the accompanying commit. OVS is the cleanest first move because its
sidecar has **no static block at all**: `odl_video/wsgi.py` wraps the app in
`dj_static.Cling`, which has been serving `STATIC_ROOT` from inside the WSGI
process the whole time. `STATIC_CLOUDFRONT_DIST` is unset in all three stacks, so
`STATIC_URL` is plain `/static/`.

Changes:

- `import_nginx_config=False`; deleted `files/web.conf_granian`.
- `static_path_mounts=["/src/staticfiles"]` — `BASE_DIR` is `/src` in the image
  and `STATIC_ROOT` is `/src/staticfiles`, the same emptyDir the collectstatic
  init container populates, and Granian's default `/static` route matches
  `STATIC_URL`. This moves static off the blocking thread pool and, incidentally,
  is the first production exercise of Granian static serving on a
  non-edxapp app.
- Probes and the APISix backend port move 8071 → 8073.
- The five YouTube `return 301`s become APISix `redirect` routes. nginx matched
  them longest-prefix-first, so the three individual videos get `priority: 20`,
  the two collection-wide prefixes `priority: 10`, and the `/*` passthrough stays
  at `0`.

`pulumi preview --stack CI` is clean: 6 updates, 1 delete (the `nginx-config`
ConfigMap), 129 unchanged. The webapp container picks up
`--static-path-mount /src/staticfiles`, the Service moves to 8073, and all six
routes render with the intended priorities.

### Rollout note

The Service port and the route's backend port (`servicePort` on ApisixRoute,
`backendRefs[].port` on HTTPRoute) change in the same update with no ordering
guarantee between them, so there is a short window where the route points at a
port the Service does not yet expose. Watch for 502s during the apply rather
than trying to engineer around it. This applies to every remaining app.

### local-dev

`local-dev/apps/odl-video-service/` (the only one of the seven with a Tilt
setup at the time PR #5281 landed) still ran a hand-maintained nginx sidecar
after that PR merged — its plain-YAML manifests are not generated from this
Pulumi code, so nothing kept them in sync automatically. Brought into line
here: `deployment.yaml` drops the `nginx` container and `nginx-conf` volume,
the Granian command gains `--static-path-mount /src/staticfiles`, and the
Service's `targetPort` now points at the app container's port directly. Its
local nginx config also carried a `/static/` block, gzip, and a dnt-policy 204
that the real prod sidecar never had for this app (OVS's static already came
from `dj_static.Cling`, not nginx) — dropped rather than translated to
ApisixRoute plugins, since matching prod behavior means not having them.
`configmaps/nginx.yaml` deleted; `Tiltfile` no longer loads it.

Each later stage should check its app's `local-dev/apps/<app>/` directory (if
one exists — most of the seven don't have Tilt setups yet) and apply the same
kind of change alongside the Pulumi change, not as a follow-up.

## Stage 2: ocw_studio

Done — see the accompanying commit. The first of the seven sidecar apps where
Granian actually carries the `/static/*` traffic nginx used to serve (OVS had
`dj_static.Cling` doing that already; edxapp LMS/Studio have been on Granian
static in production since before this project started), and the first app on
the Gateway API route path.

- `STATIC_ROOT` is the *relative* `"staticfiles"`, resolved against the image's
  `WORKDIR /src`, so it lands on the same `/src/staticfiles` emptyDir the
  collectstatic init container populates. `STATIC_URL` is `/static/`, which is
  Granian's default route, so no route override is needed.
- `hash.txt` is load-bearing here, not vestigial: the Dockerfile writes
  `$GIT_REF` into `/src/static/hash.txt` and `useAppVersionCheck` polls
  `/static/hash.txt` on every route change to force a reload after a deploy. Its
  APISix route sets `Cache-Control: private, no-cache`, which is what nginx's
  `expires -1` plus `add_header Cache-Control private` produced. The fetch
  already passes `cache: "no-store"`, so this only matters for intermediaries —
  and ocw_studio sits directly behind APISix with no CDN, so there are none
  today.
- The `/static/*` CORS route is *not* redundant here: ocw_studio has no shared
  plugin config, so without it the header the sidecar added simply disappears.
- Probes and the Service port move 8071 → 8073 via the component; the four
  HTTPRoute rules name `application_lb_service_port` explicitly (see the
  named-port trap above).

`pulumi preview --stack CI`: 3 creates (one `PluginConfig` per new route), 8
updates, 1 delete (the `nginx-config` ConfigMap), 104 unchanged. The webapp
container drops the nginx sidecar and picks up `--static-path-mount
/src/staticfiles --static-path-expires 315360000`.

## Stage 3: xpro

Done — see the accompanying commit. Same shape as ocw_studio (single
`/src/staticfiles` directory, no shared CORS plugin config), and the first app
where the nginx source and the thing Granian now serves genuinely differ:
xpro's `hash.txt` block had no `try_files`, so nginx resolved it against
`root /src` to `/src/static/hash.txt` — the source tree, not the collectstatic
output. Granian's `static_path_mounts` serves `/src/staticfiles/hash.txt`
instead.

Unlike ocw_studio, this is **not** confirmed to carry live content. Checked
the [mitxpro Dockerfile](https://github.com/mitodl/mitxpro/blob/master/Dockerfile),
[`webpack.config.prod.js`](https://github.com/mitodl/mitxpro/blob/master/webpack.config.prod.js),
and a full repo tree search for `hash` — none of them create a `hash.txt`
anywhere in the image except
[`bin/pre_compile`](https://github.com/mitodl/mitxpro/blob/master/bin/pre_compile),
which does `echo $SOURCE_VERSION >$BUILD_DIR/static/hash.txt`. That script is
a Heroku buildpack hook (`bin/compile`-syntax, `$BUILD_DIR`/`$SOURCE_VERSION`
are buildpack API variables) — nothing in the current Dockerfile or CI
workflows invokes it, so it is legacy and not part of the Docker image build.
The conclusion stands: this route is most likely already a 404 in production
today, before and after this change, and the APISix rule is kept purely for
behavioral parity with the nginx block rather than because anything is known
to depend on it. Flagged for the reviewer rather than asserted as verified.

- `import_nginx_config=False`; deleted `files/web.conf_granian` (the actually
  loaded config — `nginx_config_filename` defaults to that name — `files/web.conf`
  was already-dead uwsgi-era cruft predating this project and is left alone,
  matching the ocw_studio precedent).
- `static_path_mounts=["/src/staticfiles"]`, `static_path_expires=STATIC_ASSET_MAX_AGE_SECONDS`.
- Four HTTPRoute rules (`static-hash`, `static`, `dnt-policy`, `passthrough`),
  all naming `application_lb_service_port` and all with
  `backend_import_nginx_config=False` per the named-port trap.

`pulumi preview --stack CI --diff`: 3 creates (one `PluginConfig` per new
route), 6 updates, 1 delete (the `nginx-config` ConfigMap), 144 unchanged
(plus one incidental `Job` replace from using `XPRO_DOCKER_TAG=latest` for the
preview instead of the pinned digest — not from this change). The webapp
container drops the nginx sidecar, picks up `--static-path-mount
/src/staticfiles --static-path-expires 315360000`, and the Service/probe ports
move 8071 → 8073, all consistent with the ocw_studio precedent.

## Stage 4: micromasters, learn_ai

Done — see the accompanying commit. This is the pair gated on gap 5, now closed.

### Gap 5, resolved: no fallthrough, and it doesn't matter

Cloned `granian` at v2.7.4 (the version pinned in these images) to read the
static-serving implementation directly rather than continue treating this as
untested. Two things settle it:

- `granian/server/common.py::_init_static_mounts` requires
  `len(paths) == len(routes)` whenever more than one `--static-path-mount` is
  given, and raises `ConfigurationError('static_path')` otherwise. There is no
  way to register two mounts against the same route at all — the CLI has no
  notion of a priority-ordered list for one path. `GranianConfig.static_path_mounts`
  as it exists today would crash the server outright if given two entries with
  no matching `--static-path-route` per entry.
- Even granting two mounts on two literal routes, `src/files.rs::match_static_file`
  does not chain on a miss: for the first mount whose prefix matches the
  request, a `NotFound` on that mount's directory returns immediately
  (`return Some(Err(err.into()))`), before the loop ever reaches a second
  entry. nginx's `try_files $uri $uri/ /staticfiles/$1 ...` semantics — try
  one directory, fall back to another on 404 — have no Granian equivalent.

So the two-directory fallback nginx did (`root /src`, first try `/src/static/<path>`,
then `/src/staticfiles/<path>`) can't be reproduced. It doesn't need to be:
both apps run `init_collectstatic=True`, which populates `/src/staticfiles`
from an init container before the app container ever starts, so in this
deployment the first tier (`/src/static/<path>`, the pre-collectstatic source
tree) is dead code — it only mattered in the Heroku/local-dev path these nginx
configs were written for (learn_ai's `web.conf` says as much in its own header
comment), where nothing guaranteed collectstatic had run first. A single
`static_path_mounts=["/src/staticfiles"]`, the same shape as ocw_studio/xpro,
is behaviorally equivalent in production.

- micromasters (`OLApisixRoute`, no shared plugin config, has `hash.txt`):
  `static_path_mounts=["/src/staticfiles"]`,
  `static_path_expires=STATIC_ASSET_MAX_AGE_SECONDS`, `import_nginx_config=False`.
  Added `static-hash`, `static` (CORS), and `dnt-policy` routes, same shape as
  xpro. The three explicit `probe_configs` (kept for the django-health-check
  `Host` header override, #4874) move 8071 → 8073 by hand, since an app that
  restates `probe_configs` opts out of the component's auto-derivation.
  `backend_service_port` on all four routes moves from the `"http"` name to
  the numeric `DEFAULT_WSGI_PORT`, matching the odl_video_service precedent
  for the `ApisixRoute` CRD path (not required the way it is on
  `OLApisixHTTPRoute` — `servicePort` here resolves a k8s Service's named port
  correctly either way — but consistent and explicit is worth it once the
  sidecar is gone).
- learn_ai (`OLApisixRoute`, ASGI/websocket, `enable_defaults=True` shared
  plugin config already supplying `cors`, no `hash.txt`): `GranianConfig` and
  `import_nginx_config` are both gated on the existing `use_granian` Pulumi
  config flag (`true` in every deployed stack today, but the `False` branch
  still runs bare `uvicorn` with no static handling of its own and needs to
  keep its sidecar). No new `/static/*` route needed — `/static/*` already
  reaches the backend through the existing wildcard routes (`passauth`,
  `reqauth`, `websocket`), which pick up Granian's built-in static handling
  for free, and CORS on those routes already comes from the shared plugin
  config, unaffected by nginx either way. Added one `dnt-policy` mock route,
  on `learn_ai_https_apisix_route` only: that resource's `/*` wildcard is the
  only route (of the two ApisixRoute resources this app has) that
  `/.well-known/dnt-policy.txt` ever reached — the other resource's routes
  all require an `/ai/` prefix, so that path already 404s at the gateway
  before touching the backend and needs no new route.

One implementation note for anyone editing an existing `OLApisixRoute`'s
`route_configs`: the whole `http` list is one field on a single
`ApisixRoute` CustomResource, and Pulumi diffs plain list fields positionally.
Inserting new entries *before* existing ones shows every existing entry after
the insertion point as a spurious rename/full-field-replace in the diff (the
applied end state is still correct — priority in the CRD spec, not list
order, governs precedence — but the diff is unreadable). Appending new routes
after the existing ones keeps the diff to genuine additions plus whatever
fields actually changed.

`pulumi preview --stack CI --diff`: micromasters is 10 updates, 1 delete (the
`nginx-config` ConfigMap), 114 unchanged; learn_ai is 9 updates, 1 delete, 101
unchanged. Both webapp containers drop the nginx sidecar and pick up
`--static-path-mount /src/staticfiles --static-path-expires 315360000`;
Service and probe ports move 8071 → 8073 on both (by hand on micromasters,
auto-derived on learn_ai, which passes no explicit `probe_configs`).

## Stage 5: mit_learn, mitxonline (final)

Done — see the accompanying commit. Both apps run behind the same `use_granian`
Pulumi config flag pattern as learn_ai (`true` in every deployed stack today,
`false` still running bare `uwsgi`/true-uwsgi with no static handling of its
own and keeping its sidecar), and both are on the `OLApisixRoute` CRD path, not
Gateway API HTTPRoute, so the named-port trap doesn't apply here either.

One thing worth naming since it wasn't obvious from `import_nginx_config_path`
alone: both apps pass `import_nginx_config_path="files/web.conf_uwsgi"`
unconditionally, which looks like the active config, but it is only consulted
in the `granian_config is None` branch (`OLApplicationK8s.__init__`). Whenever
`granian_config` is set -- true in every deployed stack -- the component uses
`gc.nginx_config_filename` instead, which defaults to `"web.conf_granian"` and
neither app overrides. So `web.conf_granian` (the file this stage's
`import_nginx_config=False` makes dead) was the one actually live in
production; `web.conf_uwsgi` stays, since it's still the config the dormant
`False` branch would load.

- **mit_learn**: `GranianConfig` gains `static_path_mounts=["/src/staticfiles",
  "/src/django_media"]` paired with `static_path_routes=["/static", "/media"]`
  (gap 1, closed) and `static_path_expires`. `import_nginx_config` gates on
  `not use_granian`, matching learn_ai. Both `OLApisixRoute` resources
  (`_no_prefix` on `/*`, prefixed on `/learn/*`) already reach `/static/*` and
  `/media/*` through their existing `passauth`/`reqauth` wildcard routes, which
  attach `learn_external_service_shared_plugins` -- `enable_defaults=True`
  already supplies a default `cors` plugin, so no new CORS route was needed
  (unlike micromasters/xpro, which had no shared plugin config). Added one
  `dnt-policy` route, on the `_no_prefix` resource only: the prefixed
  resource's `/learn/*` requirement meant `/.well-known/dnt-policy.txt` never
  reached the sidecar through it either.

  JSON gzip (gap: "moving JSON gzip to the APISix gzip plugin"): a separate,
  already-merged initiative had added `OLApisixSharedPluginsConfig.enable_gzip`
  (broader content-type coverage, tuned buffers) specifically to replace this
  kind of per-app nginx `gzip` block, but defaults it to on everywhere except
  Production pending a soak test of the CPU cost on the gateway's HPA.
  mit_learn's shared plugin config didn't override that default. Simply
  dropping the sidecar would have silently lost Production's JSON compression
  (nginx's own gzip block ran unconditionally in every stack, Production
  included) until that separate soak-test gate flips independently of this
  PR. Decided with the user: set `enable_gzip=True` explicitly on mit_learn's
  shared plugin config now, to preserve exact parity with the sidecar's
  behavior across every stack as part of this migration, rather than
  inheriting a temporary Production regression from an unrelated rollout
  gate. No-op in CI/QA, where the per-stack default already resolved to
  `True` -- confirmed via `pulumi preview` diff isolation (see below).

- **mitxonline**: `GranianConfig` gains `static_path_mounts=["/src/staticfiles"]`
  and `static_path_expires` -- single mount, same shape as ocw_studio/xpro.
  `import_nginx_config` gates on `not use_granian`. Both `OLApisixRoute`
  resources (`_direct` on `/*`, `_prefix` on `/{api_path_prefix}/*`) already
  get CORS from `mitxonline_shared_plugins` (`enable_defaults=True`), so only
  `static-hash` (both resources -- `/static/hash.txt` is reachable through
  both the unprefixed and the prefixed wildcard) and `dnt-policy` (`_direct`
  only, same "prefix requirement makes it unreachable through `_prefix`"
  reasoning as mit_learn) were added.

### Verifying against a CI stack with unrelated pre-existing drift

`pulumi preview --stack CI` for both apps showed substantial changes that
predate this branch entirely -- a stale `ApisixPluginConfig` still holding an
old `oidc_error_recovery`/`serverless-pre-function` plugin the shared-plugins
component no longer emits, since-renamed Fastly-header-matching routes
(`fastly-passauth` → `passauth`, etc.), and a since-removed `browser-passauth`
route -- none of which exist anywhere in current source. Confirmed this is
pre-existing CI/git drift, not something introduced here, by running the same
preview against each app's `git stash`ed (unmodified) tree: identical drift
appears with zero code changes. Isolated this migration's actual effect by
diffing the resource-level change lines between the stashed-tree preview and
the with-changes preview for each app:

- micromasters/mit_learn/mitxonline all only add exactly the resources this
  migration touches on top of that baseline -- the `nginx-config` ConfigMap
  delete, the Service port update, and (mitxonline only, since mit_learn's
  ApisixRoute was already diffing due to the pre-existing drift) the
  ApisixRoute update for the new routes.
- mit_learn: 13 updates, 4 deletes vs. a 12-update, 3-delete baseline (net:
  +1 update folded into the already-diffing ApisixRoute, +1 delete for
  `nginx-config`).
- mitxonline: 10 updates, 1 delete vs. a 7-update, 0-delete baseline (net: +3,
  matching the three genuinely new resource-level changes above).

Both webapp containers drop the nginx sidecar and pick up the expected
`--static-path-mount`/`--static-path-route`/`--static-path-expires` args;
Service and probe ports move 8071 → 8073 on both. The rendered container-list
diff also shows what looks like a container being renamed (`nginx` →
`mitlearn-app`/`mitxonline-app`) with a `volumeMounts` entry that appears to
rename from `nginx-config` to `uwsgi-config` -- this is the same positional
list-diffing artifact noted for `OLApisixRoute` in stage 4, here on the
Deployment's `containers` list: removing the nginx container (previously
index 0) collapses the previously-index-1 app container into index 0, and
that app container already unconditionally mounted `uwsgi-config` at
`/tmp/uwsgi.ini` (`import_uwsgi_config=True`, inert whenever Granian is
actually running) before this change. Confirmed by reading
`OLApplicationK8s`'s container-list construction directly rather than
inferring from the diff text.

## Rollout order

1. ~~**odl_video_service**~~ — done, PR #5281.
2. ~~**ocw_studio**~~ — done, PR #5345.
3. ~~**xpro**~~ — done, PR #5541.
4. ~~**micromasters, learn_ai**~~ — done, PR #5549. Gap 5 (two-directory
   static fallback) closed: Granian can't reproduce it and doesn't need to.
5. ~~**mit_learn, mitxonline**~~ — done, this commit. Gap 1 (`/media` route)
   closed via `static_path_routes`; JSON gzip moved to the APISix `gzip`
   plugin. All seven apps are now sidecar-free -- this plan is complete.

## Out of scope

Static assets are served from an in-pod emptyDir rather than S3+CloudFront, even
for apps that already offload user media that way. Moving static serving to a CDN
is the more thorough fix, but it is an app-repo change and does not belong to
this project.
