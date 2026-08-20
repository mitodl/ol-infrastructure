# QA telemetry: `deployment.environment` is split-brain -- filter by `k8s.cluster.name` instead

`resource.deployment.environment` on QA spans (Tempo `grafanacloud-mitolqa-traces`) returns two
values depending on whether the emitting service uses `mitol-django-observability`:

- `rc` -- mitxonline-webapp, learn-ai-webapp, learn-webapp (mit_learn, mitxonline, xpro all set
  `MITOL_ENVIRONMENT`/`MITX_ONLINE_ENVIRONMENT` to `"rc"` for the QA stack, a historical naming
  convention that also drives the Sentry `environment` tag and the app's S3 storage bucket name --
  see `src/ol_infrastructure/applications/mit_learn/__main__.py:1159`).
- `qa` -- apisix, traefik, learn-nextjs, and the toolhive-swe services (these set
  `deployment.environment` directly from `stack_info.env_suffix`, which is `"qa"` for the QA
  stack).

**Consequence:** any query or dashboard variable scoped by `deployment.environment` only sees half
the fleet. Filtering on `"qa"` silently drops the three Django apps; filtering on `"rc"` drops the
edge and everything else.

**Fix chosen:** don't touch the env var. Renaming the app-side value to `"qa"` would also rename a
live S3 bucket (`ol-mitlearn-app-storage-rc` -> `-qa`) and move Sentry's QA environment tag, which
is a real migration, not a local config change -- not worth it for a telemetry label. Standardizing
the edge/gateway side on `"rc"` instead would just move the inconsistency to a different set of
labels (cluster name, stack name, and `ol.mit.edu/environment` k8s labels already say `"qa"`).

**Use `resource.k8s.cluster.name` for environment-scoped queries against QA (or any environment)
instead.** It's populated on every span in every service (Alloy's k8s resource detection, not
anything app-controlled) and is consistent: `applications-qa`, `operations-qa`, `residential-qa`,
etc., with no split. Confirmed empty for `resource.ol.mit.edu/environment` -- that label exists on
the k8s object, not on the span resource, so it is not usable as a filter today.

Example TraceQL, QA APISIX/Traefik/apps together:

    {resource.k8s.cluster.name="applications-qa"}

Production is unaffected by the split -- prod Tempo returns only `production` for
`deployment.environment` across every service, so this only matters for QA queries and dashboards.
