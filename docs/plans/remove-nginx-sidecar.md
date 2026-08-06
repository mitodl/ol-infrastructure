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

| | static from | `/media` | `hash.txt` | X-Forwarded-Proto | body cap | other |
|---|---|---|---|---|---|---|
| odl_video_service | *nothing* | — | — | passthrough | 500M | 5 YouTube redirects |
| ocw_studio | `/src/staticfiles` | — | yes | passthrough | 25M | |
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
Granian's CLI (verified against `granian/cli.py`) exposes `--static-path-mount`
(repeatable), `--static-path-route` (repeatable, default `/static`) and
`--static-path-expires` (default `86400`). `GranianConfig` only wires up the
first. Concretely:

1. **No `static_path_routes`.** mit_learn serves `/media/*` from
   `/src/django_media`; that needs a second route/mount pair.
2. **No `static_path_expires`.** Granian's 1-day default is a large regression
   from nginx's `expires max` (10 years) on content-hashed assets.
3. **No per-file override.** `/static/hash.txt` (`expires -1`,
   `Cache-Control: private`) is not expressible in Granian. It needs an APISix
   route with `response-rewrite`, on ocw_studio, xpro, micromasters and
   mitxonline.
4. **No CORS header on Granian-served static.** Covered by the shared plugin
   config where one is attached; must be verified per app rather than assumed.
5. **Two-directory fallback unverified.** Five configs do
   `try_files $uri $uri/ /staticfiles/$1`, i.e. `/src/<uri>` *then*
   `/src/staticfiles/<uri>`. Whether two Granian mounts on the same route fall
   through to the second on a miss is not documented and needs a live test.

`/.well-known/dnt-policy.txt → 204` needs one APISix route per app (or a decision
to drop it).

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

The Service port and the ApisixRoute `servicePort` change in the same update with
no ordering guarantee between them, so there is a short window where the route
points at a port the Service does not yet expose. Watch for 502s during the
apply rather than trying to engineer around it.

## Rollout order

1. **odl_video_service** — no static block, no shared plugin config, lowest
   traffic. Validates the port move, the redirect translation, and Granian static
   serving in one go.
2. **ocw_studio, xpro** — single static directory (`try_files /staticfiles/$1`),
   so no fallback question. Need `hash.txt` and dnt-policy routes.
3. **micromasters, learn_ai** — two-directory static fallback; gated on gap 5.
4. **mit_learn, mitxonline** — highest traffic, and mit_learn additionally needs
   the `/media` route (gap 1) and the JSON gzip moved to the APISix `gzip` plugin.

## Out of scope

Static assets are served from an in-pod emptyDir rather than S3+CloudFront, even
for apps that already offload user media that way. Moving static serving to a CDN
is the more thorough fix, but it is an app-repo change and does not belong to
this project.
