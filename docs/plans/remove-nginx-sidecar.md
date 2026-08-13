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
| xpro | `/src/staticfiles` | — | yes | passthrough | 25M | |
| micromasters | `$uri`, `/src/staticfiles` | — | yes | passthrough | 25M | |
| learn_ai | `$uri`, `/src/staticfiles` | — | — | *unset* | — | `proxy_buffering off` |
| mitxonline | `$uri`, `/src/staticfiles` | — | yes | **`$scheme`** | 25M | |
| mit_learn | `$uri`, `/src/staticfiles` | `/src/django_media` | — | **`$scheme`** | 25M | gzip on JSON |

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

1. **No `static_path_routes`.** mit_learn serves `/media/*` from
   `/src/django_media`; that needs a second route/mount pair. *Still open —
   nothing before mit_learn needs it.*
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
5. **Two-directory fallback unverified.** Five configs do
   `try_files $uri $uri/ /staticfiles/$1`, i.e. `/src/<uri>` *then*
   `/src/staticfiles/<uri>`. Whether two Granian mounts on the same route fall
   through to the second on a miss is not documented and needs a live test.
   *Still open — gates micromasters, learn_ai, mitxonline, mit_learn.*

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

## Rollout order

1. ~~**odl_video_service**~~ — done, PR #5281.
2. ~~**ocw_studio**~~ — done, this commit.
3. **xpro** — the other single-static-directory app, the same shape as
   ocw_studio. Deliberately *not* bundled with it: PR #5344 is concurrently
   changing xpro's Granian concurrency, and overlapping two live changes on one
   app makes any regression ambiguous. Do it once #5344 has landed and settled.
   Note xpro's `hash.txt` block has no `try_files`, so it resolves against
   `root /src` to `/src/static/hash.txt` — the source dir, not the collectstatic
   output. Granian will serve `/src/staticfiles/hash.txt` instead: same content,
   different file.
4. **micromasters, learn_ai** — two-directory static fallback; gated on gap 5.
5. **mit_learn, mitxonline** — highest traffic, and mit_learn additionally needs
   the `/media` route (gap 1) and the JSON gzip moved to the APISix `gzip` plugin.

## Out of scope

Static assets are served from an in-pod emptyDir rather than S3+CloudFront, even
for apps that already offload user media that way. Moving static serving to a CDN
is the more thorough fix, but it is an app-repo change and does not belong to
this project.
