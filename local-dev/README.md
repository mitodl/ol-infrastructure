# MIT Learn Local Development Environment

A fully local, Kubernetes-based development environment for the MIT Learn application stack, running in [k3d](https://k3d.io) with [Tilt](https://tilt.dev) for live development and Pulumi for shared infrastructure.

This README covers getting up and running, day-to-day usage, and troubleshooting. Two companion docs go deeper:

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — how the pieces fit together (k3d, Tilt, APISIX, the registry), written for people comfortable with Docker Compose but new to Kubernetes. Genuinely helpful background, not required reading.
- **[EXTENDING.md](EXTENDING.md)** — adding a new app to the stack or modifying shared infrastructure.

---

## Table of Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Quick Start](#quick-start)
4. [Starting and Stopping](#starting-and-stopping)
5. [Directory Structure](#directory-structure)
6. [Working with Apps](#working-with-apps)
7. [Seeding Data](#seeding-data)
8. [Configuration Reference](#configuration-reference)
9. [Disk Management](#disk-management)
10. [Teardown](#teardown)
11. [Customization & Advanced Setup](#customization--advanced-setup)
12. [Troubleshooting](#troubleshooting)

---

## Overview

This environment runs the MIT Learn application stack as Kubernetes workloads inside a local k3d cluster:

| App | Local URL | Description |
|-----|-----------|-------------|
| mit-learn (frontend) | `https://learn.mit.dev` | Next.js frontend |
| mit-learn (backend) | `https://api.learn.mit.dev` | Django/granian API |
| learn-ai | `https://ai.learn.mit.dev` | Django AI proxy service |
| mitxonline | `https://mitxonline.mit.dev` | MITx Online LMS (Django/uwsgi) |
| odl-video-service | `https://video.odl.mit.dev` | ODL Video Service (Django/uwsgi) |
| Keycloak SSO | `https://sso.ol.mit.dev` | Identity provider (olapps realm) |
| Mailpit | `https://mail.mit.dev` | Captured outbound email (web UI) |
| Grafana | `https://grafana.mit.dev` | Logs from every service in the cluster (1-week retention) |

All hostnames use a `.dev` TLD that mirrors production (`.edu` → `.dev`), so URLs, CSRF cookies, and OIDC redirect URIs behave identically to deployed environments.

**Design goals:**
- `setup.sh` does the minimum necessary outside the cluster (k3d, certs, /etc/hosts). Everything else is in-cluster.
- The shared services (database, SSO, ingress, search, …) are installed by Pulumi, the app deployments by Tilt — you don't manage either by hand. (New to Pulumi? See [ARCHITECTURE.md](ARCHITECTURE.md#pulumi--the-shared-services-installer).)
- Live source sync for Django apps (no container rebuild needed for Python changes).
- Pre-built image fallback when a source repo is not checked out.

---

## Prerequisites

Install these tools before running setup. The install commands shown are Homebrew (macOS); on Linux, install each tool from your distro's package manager or the linked docs — the stack runs on macOS, Linux, and Windows via WSL2 (see below).

| Tool | Version | Install |
|------|---------|---------|
| Docker Desktop, OrbStack, or Docker Engine (Linux) | 12–16 GB RAM for the VM (see note) | https://docs.docker.com/desktop/ or https://orbstack.dev/ |
| kubectl | ≥ 1.28 | `brew install kubectl` |
| k3d | ≥ 5.9 (tested; `setup.sh` uses `k3d registry create --image -v`) | `brew install k3d` |
| Tilt | ≥ 0.33 | https://docs.tilt.dev/install.html |
| Helm | ≥ 3.14 | `brew install helm` |
| mkcert | ≥ 1.4 | `brew install mkcert` |
| Pulumi CLI | ≥ 3.x | `brew install pulumi` |
| bash | ≥ 4 | `brew install bash` (stock macOS ships 3.2; the seeding and prune scripts use `mapfile`) |
| uv | ≥ 0.9.3 | `brew install uv` |

> **Docker memory:** Allocate at least 12 GB to the Docker VM; 16 GB is comfortable for the full stack (Docker Desktop: Settings → Resources; OrbStack allocates dynamically; Docker Engine on Linux uses host RAM directly — nothing to allocate). Usage scales with how many apps you enable and whether they run from source or prebuilt images: with just mit-learn (from source) and mitxonline enabled, measured pod usage is ~13 GB, the Next.js dev server alone accounting for 3–4 GB. To see what's actually using memory: `kubectl top pods -A | sort -k4 -hr | head`.

### Windows (WSL2)

Install all tools listed above **inside WSL** (not on Windows), then note these additional requirements:

- **Docker Desktop WSL integration:** Settings → Resources → WSL Integration → enable your distro. The k3d cluster runs inside WSL; Docker Desktop forwards `127.0.0.1` to Windows so browsers can reach it.
- **`/etc/hosts` persistence:** WSL regenerates `/etc/hosts` by default on every restart. `setup.sh` adds `generateHosts = false` to `/etc/wsl.conf` automatically. If it does, run `wsl --shutdown` in Windows PowerShell then reopen your terminal before proceeding.
- **Windows hosts file:** Your Windows browser resolves DNS from `C:\Windows\System32\drivers\etc\hosts`, not WSL's `/etc/hosts`. `setup.sh` attempts to update the Windows hosts file automatically; if it cannot (the file requires Windows admin elevation), it prints the entries and an `Add-Content` PowerShell command to paste into an elevated terminal.
- **TLS trust on Windows:** The mkcert root CA installed in WSL is not trusted by Windows. After running `setup.sh`, run the `certutil` command it prints in an **elevated** Windows PowerShell to add the CA to the Windows Root certificate store, then restart your browser.

---

## Quick Start

### 1. One-time bootstrap

From the `ol-infrastructure` repo root:

```bash
./local-dev/scripts/setup.sh
```

This will:
1. Check all prerequisites
2. Create the `local-dev` k3d cluster with a local image registry on port 5001
3. Generate a wildcard TLS certificate with `mkcert` (trusted by your OS)
4. Add all `.dev` hostnames to `/etc/hosts` (requires `sudo`)

> **WSL2 users:** If `setup.sh` reports that `/etc/wsl.conf` was updated, run `wsl --shutdown` in Windows PowerShell and reopen your WSL terminal before continuing. The script also prints any Windows hosts entries or a `certutil` command that need to be applied in an elevated Windows PowerShell.

### 2. Configure Tilt

```bash
cp tilt_config.json.example tilt_config.json
# Edit tilt_config.json — see Configuration Reference below
```

At minimum, review `enabled_apps` to enable only the services you need.

For per-developer app env vars and secrets (API keys, feature flags), don't edit the tracked manifests — drop a gitignored `app-env.local.yaml` ConfigMap next to the app's tracked one instead. See [Local Configuration Overrides](#local-configuration-overrides).

### 3. Start the environment

```bash
./local-dev/scripts/start.sh
```

This will:
1. Validate that `setup.sh` has been run (cluster exists, kubeconfig configured, certs present)
2. Restart the k3d cluster if it was paused by `stop.sh`
3. Heal any wedged kubelet exec/streaming (see [Troubleshooting](#kubectl-exec-fails-with-a-502-wedged-kubelet-streaming)) — a no-op when healthy
4. Sync Python dependencies via `uv`
5. Start Tilt

### 4. Monitor the environment

Tilt will:
1. Run `pulumi up` to deploy shared infrastructure (Postgres, Keycloak, APISIX, etc.)
2. Build Docker images for any checked-out app repos
3. Apply all app manifests (Deployments, Services, ConfigMaps, APISIX routes)
4. Watch for source changes and sync them live

Open the Tilt UI at `http://localhost:10350` to monitor deployments and trigger seeds.

---

## Starting and Stopping

The stack has two independently-running halves, and knowing which is which answers most lifecycle questions:

- **The cluster** (k3d node containers + every pod inside them) runs *detached*, like `docker compose up -d`. It needs no terminal and survives you closing everything.
- **Tilt** (`start.sh` / the `tilt up` terminal) is the *dev loop*: the UI at localhost:10350, file-watching and live-sync, automatic rebuilds, and the disk-janitor. It runs in the foreground, on purpose — there is no detached mode.

Day-to-day:

| You want to… | Do this | What happens |
|---|---|---|
| Start working | `./local-dev/scripts/start.sh` | Restarts the cluster if paused, heals, syncs deps, runs Tilt |
| Stop coding but keep apps running | `Ctrl+C` in the Tilt terminal | Apps stay reachable at their `.dev` URLs; edits no longer sync until Tilt runs again |
| Shut down for the day | `Ctrl+C` Tilt, then `./local-dev/scripts/stop.sh` | Removes Tilt-managed workloads, pauses the cluster; DB data survives |
| Destroy everything | `./local-dev/scripts/teardown.sh` | Deletes the cluster, Pulumi resources, certs, /etc/hosts entries |

Closing the Tilt terminal (or Ctrl+C) never deletes anything from the cluster — pods keep running because Kubernetes, not Tilt, keeps them alive. The one thing to remember: with Tilt stopped, a pod that gets recreated comes up from its last-built image *without* your live-synced edits; they re-sync when Tilt next runs.

---

## Directory Structure

The files you'll actually touch:

```
ol-infrastructure/
├── Tiltfile                     # Root entry point; wires all app Tiltfiles
├── tilt_config.json             # Your dev config (copy of tilt_config.json.example)
│
└── local-dev/
    ├── scripts/                 # setup.sh, start.sh, stop.sh, teardown.sh, seed.sh,
    │                            # heal-exec.sh, prune-docker.sh (each described in its header)
    ├── cluster/                 # k3d cluster definition + registry retention config
    ├── certs/                   # mkcert output (gitignored)
    ├── infra/                   # Pulumi stacks: shared in-cluster infra (see EXTENDING.md)
    └── apps/<app>/              # Per-app k8s manifests + Tiltfile
        └── configmaps/
            └── app-env.local.yaml   # (optional, gitignored) your env overrides — see
                                     # Local Configuration Overrides below
```

---

## Working with Apps

### Enable/disable apps

Edit `tilt_config.json`:

```json
{
  "enabled_apps": ["mit-learn", "learn-ai"]
}
```

Only the listed apps will be deployed. Shared infrastructure always runs.

### Test accounts

Log in at any app (or `https://sso.ol.mit.dev` directly) with the seeded Keycloak users: `admin@odl.local`, `student@odl.local`, `prof@odl.local` — password `localdev123` for all three.  <!-- pragma: allowlist secret -->

### Editing code

With an app repo checked out next to `ol-infrastructure`, Tilt live-syncs your edits into the running containers — no rebuild. (Curious how? See [Two transports](ARCHITECTURE.md#two-transports-how-your-code-reaches-a-pod) in ARCHITECTURE.md.) What to expect:

- **Django apps:** granian runs with `--reload` and restarts its workers on each change — the new code serves once Django finishes re-importing (roughly 10–30 s depending on the app). Watch for `Changes detected, reloading workers..` in the app logs. Celery workers and beat don't auto-reload — restart those resources from the Tilt UI after changing task code. When `pyproject.toml` or `uv.lock` changes, Tilt runs `uv sync` inside the container first.
- **Next.js frontend:** Tilt builds the `local-dev` stage of `Dockerfile.web`, which runs `next dev` (Turbopack). Changes under `frontends/` hot-reload in roughly a second (the first request to each page after a pod start pays a one-time on-demand compile). HMR websockets are proxied through apisix, so the browser hot-updates on `https://learn.mit.dev` too. `NEXT_PUBLIC_*` values are runtime env vars (see its `configmaps/app-env.yaml`), not build args, so changing them needs no rebuild. When `yarn.lock` changes, Tilt runs `yarn install` inside the container.
- **No checkout:** Tilt deploys the pre-built Docker Hub image (`mitodl/<app>-app`) at the tag listed in `tilt_config.json` under `prebuilt_tags`. It works, but does not hot-reload — you can work on mit-learn without having learn-ai checked out.

### Access logs

For a single pod you already have in mind, `kubectl` is the shortest path:

```bash
# Web pod
kubectl logs -n mit-learn deploy/mitlearn-webapp -c app -f

# Celery worker
kubectl logs -n mit-learn deploy/mitlearn-worker-default -f

# APISIX ingress
kubectl logs -n operations deploy/apisix -f
```

To search across services — or to follow a request from the ingress into the app and on into the worker that picked up the task — use **Grafana at [https://grafana.mit.dev](https://grafana.mit.dev)**. It opens straight into the UI with no login. Every pod in the cluster is collected, including the ones Tilt does not manage (Postgres, Valkey, OpenSearch, Keycloak, APISIX, Mailpit).

Start from the pre-provisioned **local-dev logs** dashboard, or go to **Explore → Loki** and write LogQL directly:

```logql
{namespace="mit-learn"}                      # everything in one app's namespace
{app="mitlearn-webapp"} |= "ERROR"           # one workload, filtered
{namespace="operations", app="apisix"}       # ingress access logs
{container="celery"} |= "Traceback"          # across every app's workers
```

Available labels are `namespace`, `pod`, `container`, and `app`. Logs are kept for one week by default — see [Log retention](#log-retention) to change that, and `observability_enabled` in [Pulumi stack config](#pulumi-stack-config) to turn the whole stack off.

### Run management commands

```bash
kubectl exec -it -n mit-learn deploy/mitlearn-webapp -c app -- python manage.py shell
kubectl exec -it -n learn-ai deploy/learnai-webapp -c app -- python manage.py dbshell
```

### Run tests

Python tests need the app's environment (database, settings), so run them inside the container — they run against the code Tilt has live-synced, so they see your latest edits:

```bash
kubectl exec -it -n mit-learn deploy/mitlearn-webapp -c app -- pytest main/envs_test.py
```

Frontend (jest) tests don't need the cluster at all — run them from your mit-learn checkout on the host, if you have its JS toolchain set up there (node + corepack + `yarn install`):

```bash
cd ../mit-learn && yarn test useToggle   # the argument is a jest pattern
```

No host toolchain? The dev container has everything installed — run them there instead:

```bash
kubectl exec -it -n mit-learn deploy/mitlearn-nextjs -- sh -c 'cd /app && yarn test useToggle'
```

### Connect to PostgreSQL directly

```bash
kubectl exec -it -n local-infra local-pg-1 -- psql -U app -d mitlearn
```

### Inspect emails (Mailpit)

All outbound email is captured by Mailpit. Access the web UI at `https://mail.mit.dev`.

---

## Seeding Data

Bootstrap seeds (migrations, `collectstatic`, fixture loads) run automatically in the `initContainer` on first deploy. Additional enrichment seeds are available as **manual Tilt resources** — they never run automatically.

### From the Tilt UI

In the Tilt UI at `http://localhost:10350`, find resources labeled `seed` and click the play button.

### From the command line

```bash
# Trigger a specific seed
tilt trigger seed-mit-learn-fixtures

# Or use seed.sh directly
./local-dev/scripts/seed.sh --app mit-learn
./local-dev/scripts/seed.sh --app mit-learn --cmd "backpopulate_ocw_data"
./local-dev/scripts/seed.sh --list
```

### Available seeds per app

| App | Seed label | What it does |
|-----|-----------|--------------|
| mit-learn | `seed-mit-learn-fixtures` | Load platforms, schools, departments, offered_by |
| mit-learn | `seed-mit-learn-qdrant` | Create Qdrant vector collections |
| mit-learn | `seed-mit-learn-opensearch` | Recreate OpenSearch index |
| mit-learn | `seed-mit-learn-ocw` | Backpopulate OCW learning resources |
| mit-learn | `seed-mit-learn-mitxonline` | Backpopulate MITx Online resources |
| mit-learn | `seed-mit-learn-featured-lists` | Create dev-only featured lists per offeror channel |
| learn-ai | `seed-learn-ai-checkpoints` | Backpopulate tutor checkpoints |
| mitxonline | `seed-mitxonline-instance` | Full instance setup (superuser, courses, products) |
| mitxonline | `seed-mitxonline-course-data` | Populate test course data |
| mitxonline | `seed-mitxonline-income-thresholds` | Load country income thresholds |
| odl-video-service | `seed-ovs-presets` | Create encoding presets (requires real AWS creds) |

---

## Configuration Reference

`tilt_config.json` is your copy of [`tilt_config.json.example`](../tilt_config.json.example) — the example file is the canonical starting point (its image tags move over time; this doc doesn't repeat them). Keys:

| Key | Default | Description |
|-----|---------|-------------|
| `enabled_apps` | all four | Apps to deploy. Omit any to skip it entirely. |
| `prebuilt_tags` | see example file | `["app=tag"]` list of image tags used when the app repo is not checked out locally. |
| `disk_keep_tags`, `disk_buildcache_max_gb` | `3`, 10% of disk | Disk retention knobs — see [Disk Management](#disk-management). |
| `log_retention_period` | `168h` | How long Grafana/Loki keeps logs — see [Log retention](#log-retention). |
| `per_app_databases`, `openedx_mode` | — | Declared but not wired to anything yet; setting them has no effect. |

The rule of thumb for which config surface a knob belongs to: settings that change **which/how Tilt runs things** (apps, image tags) go in `tilt_config.json`; anything that sets an **env var or secret value inside a workload** (API keys, feature flags, endpoints) goes in a gitignored `app-env.local.yaml` override ConfigMap — see [Local Configuration Overrides](#local-configuration-overrides).

### Root domain

Every service hostname derives from the `LOCAL_DEV_ROOT_DOMAIN` environment variable, which defaults to `mit.dev`: `learn.<root_domain>`, `api.learn.<root_domain>`, `sso.ol.<root_domain>`, and so on. Set it in your shell environment so that it is exported to `tilt up` and `setup.sh`.

`tilt up` fails immediately if `sso.ol.<root_domain>` does not resolve, whether through DNS or `/etc/hosts`.

After changing the value, re-run `./local-dev/scripts/setup.sh` to reissue the TLS certificate and rewrite the `/etc/hosts` block; pass `--skip-hosts` to reissue the certificate only, which is what you want when the hostnames already resolve through DNS. The certificate's SANs cover one root domain, so requests to hostnames outside it fail at the TLS layer.

### Log retention

Logs are kept for **one week** (`168h`). To change that for yourself, set `log_retention_period` in your gitignored `tilt_config.json`:

```json
{
  "log_retention_period": "72h"
}
```

Tilt forwards it to the core Pulumi stack as `LOCAL_DEV_LOG_RETENTION`, so the same value works for a hand-run `pulumi up`:

```bash
cd local-dev/infra/core
LOCAL_DEV_LOG_RETENTION=72h pulumi up --stack local-dev.core.Dev
```

Loki only honours a retention window that is a **whole number of days**, so give it hours in multiples of 24 (`48h`, `168h`) or days (`3d`, `7d`). Anything else fails the deploy with an explanatory error rather than being silently ignored.

This knob is deliberately *not* pinned in `Pulumi.local-dev.core.Dev.yaml` — Pulumi config takes precedence over the environment, so a value committed there would override every developer's `tilt_config.json`. Changing it replaces the Loki ConfigMap and restarts Loki; already-ingested logs are re-evaluated against the new window on the next compaction pass (within ~15 minutes).

### Pulumi stack config

The infrastructure is split across two Pulumi stacks:

**`local-dev/infra/core/Pulumi.local-dev.core.Dev.yaml`** — operators, Keycloak, APISIX, database, cache:

| Key | Default | Description |
|-----|---------|-------------|
| `keycloak_hostname` | `sso.ol.mit.dev` | Keycloak ingress hostname |
| `tls_cert_path` | `local-dev/certs/local-dev.pem` | mkcert cert (relative to repo root) |
| `apisix_version` | `2.12.0` | APISIX Helm chart version |
| `cnpg_version` | `0.23.0` | CloudNativePG operator Helm chart version |
| `keycloak_operator_version` | `26.0.7` | Official Keycloak Operator version |
| `observability_enabled` | `true` | Deploy Grafana + Loki + Alloy (~1.3GB). Set to `false` on a constrained Docker VM. |

**`local-dev/infra/apps_infra/Pulumi.local-dev.apps-infra.Dev.yaml`** — Keycloak realm and OIDC clients:

| Key | Default | Description |
|-----|---------|-------------|
| `*_client_secret` | `local-dev-*-secret` | OIDC client secrets (change if needed) |
| `apisix_oidc_session_secret` | `local-dev-oidc-session-secret-32chars!` | Session encryption key (kept for reference) |

---

## Disk Management

Every Tilt image build produces a multi-GB image in **three places**: the local Docker daemon, the k3d registry (`k3d-registry.localhost:5001`), and — once pulled — each k3s node's internal containerd store. Tilt's built-in pruner (`docker_prune_settings`) has silent failure modes and by design only reaches the first[^tilt-pruner]. Left alone, these stores grow by several GB per rebuild until kubelet taints every node with `disk-pressure` and no pod can schedule.

[^tilt-pruner]: Registry cleanup is [tilt-dev/tilt#2102](https://github.com/tilt-dev/tilt/issues/2102); node-store cleanup is [tilt-dev/tilt#4228](https://github.com/tilt-dev/tilt/issues/4228).

Each store is bounded by retention config owned by the component that enforces it, with no per-developer setup:

| Mechanism | Covers | Where |
|---|---|---|
| `disk-janitor` (automatic, runs with every `tilt up`) | Old tilt-built image tags in the local daemon; build-cache size cap; registry repos left manifest-less by an interrupted push | `local-dev/scripts/disk-janitor.sh`, wired as a `serve_cmd` resource in the root Tiltfile |
| zot registry retention + GC | The k3d registry — zot keeps the 10 most recently pushed tags per repo and garbage-collects the rest itself | `local-dev/cluster/zot-config.json` (the registry image is [zot](https://zotregistry.dev), not registry:2; created by `setup.sh`) |
| kubelet image GC | Node containerd stores | Thresholds in `local-dev/cluster/k3d-config.yaml` (applies at cluster creation; existing clusters keep the old 85/80 until you run `local-dev/scripts/migrate-kubelet-gc-thresholds.sh`) |
| `prune-docker` (manual, break-glass) | Local daemon + registry, destructively (node stores only with `--sweep-nodes` — read the script header first; it orphans running containers) | Tilt UI button / `tilt trigger prune-docker`, or run `local-dev/scripts/prune-docker.sh` directly |

**Existing setups:** a registry container created before the zot swap (2026-07) still runs `registry:2`, which has no retention and will grow unbounded — the janitor warns about this each cycle until you migrate:

```bash
k3d registry delete k3d-registry.localhost
./local-dev/scripts/setup.sh   # recreates it as zot, reconnects the cluster network
```

Registry contents are a disposable cache — Tilt re-pushes whatever the current build needs on its next build, and running pods are unaffected (nodes cache their images).

Retention (keep the newest N) is safe to apply at any moment — unlike a wipe, it can never delete an image something is about to need. Janitor knobs, via `tilt_config.json` (or env var fallback):

- `disk_keep_tags` / `LOCAL_DEV_DISK_KEEP_TAGS` — tags kept per image (default 3). Old tags are nearly pure waste: pods only reference the current tag, and rebuild speed comes from the build cache, not old tags.
- `disk_buildcache_max_gb` / `LOCAL_DEV_BUILDCACHE_MAX_GB` — build-cache cap in GB (default: 10% of total disk). **This is the one knob whose effect is not scoped to local-dev**: BuildKit keeps a single daemon-wide cache pool, so eviction can slow rebuilds of unrelated projects on your machine (speed only, never correctness). Set to `0` to opt out and manage the pool yourself (e.g. `builder.gc` in your Docker engine config).

If images ever pile up again despite the janitor, `tilt docker-prune --debug` prints Tilt's own per-image skip reasons and is the fastest way to see why something isn't being reclaimed. To check whether zot is doing its part, `docker logs k3d-registry.localhost` shows its retention decisions (`"module":"retention"` lines, logged at info level) and its GC results (`"module":"gc"`, one `gc successfully completed` per repo).

One registry case zot cannot reclaim on its own is a repo left with no manifest by an interrupted push — see [zot logs "repo metadata not found"](#zot-logs-repo-metadata-not-found-for-given-repo-name). The janitor sweeps those.

---

## Teardown

```bash
# Remove the cluster, certs, and /etc/hosts entries (default — removes everything)
./local-dev/scripts/teardown.sh

# Keep certs (useful if you want to reuse them on next setup)
./local-dev/scripts/teardown.sh --keep-certs

# Keep /etc/hosts entries
./local-dev/scripts/teardown.sh --keep-hosts

# Keep both certs and /etc/hosts entries
./local-dev/scripts/teardown.sh --keep-certs --keep-hosts
```

> **Note:** The teardown script calls `pulumi destroy` automatically to clean up Pulumi-managed resources before deleting the cluster, so no orphaned resources are left behind.

Pulumi state must never outlive the cluster: everything these stacks manage
lives inside the cluster, but the state lives in this checkout, so state that
survives makes the next `pulumi up` skip resources that no longer exist (that
is the `404 Realm not found` failure). Teardown therefore discards a stack's
state if its `destroy` fails, discards leftover state when the cluster is
already gone (deleted by hand, or by an interrupted teardown), and **stops
before `k3d cluster delete`** if it can neither destroy nor discard — the
checked-in `Pulumi.<stack>.yaml` config is preserved either way. If it stops,
it prints the exact `pulumi stack rm` to run; do that and re-run teardown.

---

## Customization & Advanced Setup

### Local Configuration Overrides

The ConfigMaps and Secrets are tracked by the repository. For per-developer customizations (API keys, feature flags, custom endpoints), each app has an optional, gitignored override ConfigMap:

```bash
cp local-dev/apps/mitxonline/configmaps/app-env.local.yaml.example \
   local-dev/apps/mitxonline/configmaps/app-env.local.yaml
# then add your overrides under data:, e.g.
#   FEATURE_IGNORE_EDX_FAILURES: "True"
```

How it works — plain Kubernetes, visible in each app's `deployment.yaml`: every container's `envFrom` list references the override ConfigMap (`mitxonline-env-local` etc.) **last** and with `optional: true`. Kubernetes resolves duplicate `envFrom` keys by letting the last source win, so your overrides beat both the tracked ConfigMap *and* the tracked Secret — secret values are fine in this file, it never leaves your machine. `optional: true` means no file → no ConfigMap → no-op for everyone else.

Day-to-day behavior:

- Creating or editing the file mid-session re-applies it — no Tilt restart. Pods roll automatically so new values actually take effect: because Kubernetes does not restart pods on ConfigMap/Secret changes, `tiltlib.star` stamps a fingerprint of every applied ConfigMap's and Secret's data onto each Deployment's pod template (the `ol.mit.edu/config-hash` annotation) — this covers edits to the tracked `app-env.yaml`/`secrets.yaml` too, not just this override file — which is also the idiomatic production pattern (cf. Helm `checksum/config` annotations).
- Overridden key **names** (never values) are printed in the **Tiltfile resource's log** in the Tilt UI, and your full delta is inspectable in-cluster at any time: `kubectl get cm -n mitxonline mitxonline-env-local -o yaml`
- Gotchas: the ConfigMap's `metadata.name` must match what `deployment.yaml` references (`<app>-env-local` — copy the example, don't type it), and all `data:` values must be YAML **strings** (quote things like `"True"` and `"8080"`). A typo'd key is applied but ignored by the app. If you *delete* the file mid-session, prefer emptying `data:` instead — the already-applied ConfigMap can linger in-cluster until `tilt down`.

Every app supports this, including **mit-learn-nextjs** (e.g. per-developer PostHog credentials — see its `app-env.local.yaml.example`).

Scope notes:

- **OpenAI key for LiteLLM** (`local-infra` namespace, Pulumi-managed) is separate from the app overlays — create the Secret directly (the deployment marks it optional, so this is safe to skip entirely):
  ```bash
  kubectl create secret generic litellm-secrets -n local-infra \
    --from-literal=openai_api_key=sk-your-key  # pragma: allowlist secret
  ```
  (That `openai_api_key` Secret *data key* is unrelated to the old `tilt_config.json` key of the same name, which was never wired to anything and has been removed.)

### GPU Support for Ollama

If you prefer to run Ollama on your host machine to use GPU acceleration:

1. **Stop the in-cluster Ollama** — point `OLLAMA_ENDPOINT` at your host via the app's gitignored `app-env.local.yaml` (see [Local Configuration Overrides](#local-configuration-overrides)):
   ```yaml
   OLLAMA_ENDPOINT: "http://host.docker.internal:11434"
   ```
   (Docker Desktop; on Linux use `http://172.17.0.1:11434`.)

2. **Run Ollama on your host:**
   ```bash
   ollama serve  # Listens on localhost:11434 by default
   ```

### Custom S3 Storage (MinIO / RustFS)

The local-dev stack doesn't include S3 storage by default. To add it:

**Option 1: Use external MinIO instance** — Run MinIO on your host and point apps at it via their gitignored `app-env.local.yaml` (see [Local Configuration Overrides](#local-configuration-overrides)):
```yaml
AWS_ENDPOINT_URL: "http://host.docker.internal:9000"
AWS_ACCESS_KEY_ID: "minioadmin"  # pragma: allowlist secret
AWS_SECRET_ACCESS_KEY: "minioadmin"  # pragma: allowlist secret
```
(Docker Desktop; on Linux use `http://172.17.0.1:9000`.)

**Option 2: Deploy MinIO in-cluster** — Add a MinIO module to the `core` Pulumi stack and patch the ConfigMaps accordingly (see [EXTENDING.md](EXTENDING.md#modifying-shared-infrastructure)).

---

## Troubleshooting

### `tilt up` fails on `local-infra` (Pulumi errors)

```bash
# Verbose core stack run
cd local-dev/infra/core
PULUMI_CONFIG_PASSPHRASE='' pulumi up --stack local-dev.core.Dev --logtostderr -v=3

# Verbose apps_infra stack run
cd local-dev/infra/apps_infra
PULUMI_CONFIG_PASSPHRASE='' pulumi up --stack local-dev.apps-infra.Dev --logtostderr -v=3
```

Common causes:
- **kubeconfig not set:** Ensure `k3d kubeconfig merge local-dev --kubeconfig-merge-default` has been run. If your `~/.kube` directory is a symlink pointing to a Windows-side path (common in WSL2), see [WSL2: kubeconfig context not found](#wsl2-kubeconfig-context-not-found) below.
- **Cert files missing:** Run `setup.sh --skip-hosts` to regenerate certs without touching `/etc/hosts`.

### App pod stuck in `Init:CrashLoopBackOff`

The initContainer runs migrations; check its logs:

```bash
kubectl logs -n <namespace> <pod-name> -c bootstrap
```

Common causes:
- Database not ready yet (CNPG takes ~30s on first run — Tilt will retry automatically).
- Missing required env var — check the configmap and secrets against the app's `settings.py`.

### APISIX returns 404 for a hostname

```bash
# Check that ApisixRoute was picked up
kubectl get apisixroute -n <namespace>

# Check APISIX ingress controller logs
kubectl logs -n operations deploy/apisix-ingress-controller -f
```

The ingress controller watches for `ApisixRoute` CRDs and syncs them to the APISIX data plane. A restart of the ingress controller pod often resolves sync issues.

### Keycloak login loop / OIDC errors

Keycloak takes 60–90 seconds to start on first boot (database schema migration). Check its readiness:

```bash
kubectl get pod -n local-infra -l app=keycloak
kubectl logs -n local-infra -l app=keycloak -f
```

Also verify the `olapps` realm was provisioned by Pulumi:

```bash
cd local-dev/infra/apps_infra
PULUMI_CONFIG_PASSPHRASE='' pulumi stack output --stack local-dev.apps-infra.Dev
```

### TLS certificate not trusted

```bash
mkcert -install   # Install the mkcert root CA into your OS trust store
```

Then restart your browser. The cert was generated with the correct wildcard SANs but the root CA must be in your OS trust store.

### Docker image build fails (Next.js)

The Next.js build needs ~4 GB of memory. If it OOMs:
- Increase the Docker VM memory allocation (see [Prerequisites](#prerequisites))
- Or use a prebuilt image by removing `mit-learn` from `enabled_apps` in `tilt_config.json` and letting Tilt use the `prebuilt_tags` value instead

### Image push retries forever on the last layer

Tilt's push stalls partway through one big layer, restarts from zero, and eventually gives up:

```
89076705fa1d: Pushing [==================>       ]  1.156GB/3.169GB
```

zot defaults `http.readTimeout` to 60s, and Go's `ReadTimeout` is a deadline on the *entire* request including its body — not an idle timeout. A monolithic blob upload that takes longer than that is killed mid-stream no matter how fast data is flowing; zot deletes the partial upload, Docker retries the layer, and hits the same wall. The registry log shows the deadline verbatim:

```bash
docker logs k3d-registry.localhost 2>&1 | grep '"level":"error"'
# PATCH /v2/<repo>/blobs/uploads/<id>  statusCode: 500  latency: "1m0s"
# "unexpected error, removing .uploads/ files"  error: "read tcp ...: i/o timeout"
```

Small layers push fine, so this only ever bites the multi-GB `node_modules` layers — those need ~80s on a typical dev box. `zot-config.json` therefore sets `readTimeout` and `writeTimeout` to `30m`. If you see this after editing that file, confirm the running registry actually picked the values up (they need a restart, not a config hot-reload):

```bash
docker logs k3d-registry.localhost 2>&1 | grep -o '"ReadTimeout":[0-9]*' | tail -1
# "ReadTimeout":1800000000000   <- nanoseconds; 60000000000 means the default is still in effect
docker restart k3d-registry.localhost
```

### zot logs `repo metadata not found for given repo name`

The registry log shows GC failing for one repo, on every zot start and every hourly GC pass:

```
"failed to run GC for /var/lib/zot/mitodl_mit-learn-nextjs-app"
error: "repo metadata not found for given repo name"
"gc unsuccessfully completed for /var/lib/zot/mitodl_mit-learn-nextjs-app"
```

zot's GC scheduler walks the *storage directory* but its retention logic is driven by the *metadata DB*, so it enqueues a task for every repo dir on disk and then fails on any the metadata DB has never heard of. A repo gets into that state when layer uploads land but the manifest PUT that registers them never does — i.e. any interrupted push (Ctrl-C, cancelled Tilt build, or the 60s upload timeout above). The completed layers stay on disk as blobs no manifest refers to, and GC bails before it can collect them, so they are unreachable *and* un-collectable: multiple GB that nothing reclaims.

`disk-janitor.sh` sweeps these automatically (repo dirs whose `index.json` lists no manifests, untouched for 60 minutes so a live push is never at risk). To clear one immediately:

```bash
# list repos with no manifest
docker run --rm --volumes-from k3d-registry.localhost alpine sh -c \
  'for d in /var/lib/zot/*/; do grep -qE "\"manifests\":(null|\[\])" "$d/index.json" 2>/dev/null && echo "$d"; done'

# remove one (safe: with no manifest there is nothing pullable in it)
docker run --rm --volumes-from k3d-registry.localhost alpine rm -rf /var/lib/zot/<repo>
```

The push that follows re-uploads those layers. Note the error names the repo whose push broke — fixing the push (usually the timeout above) stops it recurring.

### `kubectl exec` fails with a 502 (wedged kubelet streaming)

`kubectl exec` / `attach` / `logs -f` into a pod may fail like this, even though `kubectl get` / `describe` / `logs` still work and the node shows `Ready`:

```
error: Internal error occurred: error sending request: Post
"https://192.168.97.3:10250/exec/...": proxy error from 127.0.0.1:6443
while dialing 192.168.97.3:10250, code 502: 502 Bad Gateway
```

**Why:** the API server proxies exec/attach/logs-follow to each node's kubelet on `:10250`. When that streaming server on a node gets wedged, exec 502s while ordinary kubectl keeps working (it doesn't use the streaming path). This is a known k3s/kind failure mode with several triggers; the one you'll hit most on this stack is **macOS sleep** — k3d nodes run inside the Docker VM (OrbStack or Docker Desktop), the Mac sleeping pauses that VM, and a node's kubelet can come back wedged on resume. (A Linux host that suspends could in principle do the same; a Linux box that never sleeps generally won't.) Either way, restarting Tilt does **not** help — it only recycles workloads, not the node containers.

**Fix (any platform):** run the heal script, which probes each node and `docker restart`s only the wedged ones — this clears the wedge whatever caused it, and preserves the node's IP (unlike `k3d cluster stop/start`). It's a no-op when everything is healthy:

```bash
./local-dev/scripts/heal-exec.sh
```

`start.sh` runs this automatically, so starting your session with it already covers the cold-start case on every platform.

**Automatic on wake (macOS):** to heal without thinking about it, use [sleepwatcher](https://www.bernhard-baehr.de/) to run the heal on every wake:

```bash
brew install sleepwatcher
# edit the REPO path inside the example hook first, then symlink it as ~/.wakeup:
ln -sf "$PWD/local-dev/scripts/wakeup.example.sh" ~/.wakeup
brew services start sleepwatcher
```

sleepwatcher runs `~/.wakeup` on every wake; the example hook calls `heal-exec.sh` and logs to `~/Library/Logs/local-dev-heal.log`.

**Automatic on wake (Linux):** we don't ship a hook, but if your dev box suspends and you hit this, wrap `heal-exec.sh` in a systemd resume hook — a script in `/usr/lib/systemd/system-sleep/` (invoked with `post`/`resume`) or a unit ordered `After=suspend.target`.

### macOS: Port conflict during cluster creation

The k3d registry is bound to host port 5001. This avoids the macOS AirPlay Receiver port conflict that affected port 5000 in older versions of this setup. If you see `Address already in use` on port 5001, check what is using it:

```bash
lsof -i :5001
```

To move it: change the `--port` in `setup.sh`'s `k3d registry create` call and the matching `k3d-registry.localhost:5001` entries in `local-dev/cluster/k3d-config.yaml`, then delete the registry container and re-run `setup.sh`.

### Linux: inotify limit exceeded

Tilt watches source files and uses inotify for change detection. On Linux, the default inotify limit may be too low for watching the entire Tilt workspace. If you see errors like `watch ENOSPC` or "No space left on device", increase the limit:

```bash
# Increase inotify watch limit (recommended: 100k for large workspaces)
sudo sysctl fs.inotify.max_user_watches=100000

# Make it permanent (add to /etc/sysctl.conf)
echo 'fs.inotify.max_user_watches=100000' | sudo tee -a /etc/sysctl.conf
```

### WSL2: kubeconfig context not found

If `k3d kubeconfig merge` succeeds but `kubectl config get-contexts local-dev` still fails, your `~/.kube` directory may be a Windows-side symlink. WSL2 sometimes creates `~/.kube` as a symbolic link pointing to the Windows `%USERPROFILE%\.kube` directory. When k3d writes the merged kubeconfig into WSL's path, the Windows symlink destination may not be reachable or may silently drop the context.

**Fix:** Break the symlink and create a real WSL-side directory:

```bash
# Backup and replace the symlink with a real directory
cp ~/.kube/config ~/kube-backup.yaml 2>/dev/null || true
rm ~/.kube          # Remove symlink (NOT the Windows-side directory)
mkdir -p ~/.kube
cp ~/kube-backup.yaml ~/.kube/config 2>/dev/null || true

# Re-merge k3d kubeconfig
k3d kubeconfig merge local-dev --kubeconfig-merge-default
kubectl config get-contexts local-dev   # Should now succeed
```

### `/etc/hosts` entries disappear after WSL restart

WSL2 regenerates `/etc/hosts` by default. `setup.sh` sets `generateHosts = false` in `/etc/wsl.conf` automatically, but the change only takes effect after restarting WSL. From Windows PowerShell run:

```powershell
wsl --shutdown
```

Then reopen your WSL terminal. If `setup.sh` has already run, the entries will persist from that point on.

### Windows browser can't resolve `.dev` hostnames

Your Windows browser reads `C:\Windows\System32\drivers\etc\hosts`, not WSL's `/etc/hosts`. `setup.sh` attempts to write the same block to the Windows hosts file directly. If it couldn't (requires Windows admin elevation), re-run `setup.sh` and paste the printed `Add-Content` command into an **elevated** Windows PowerShell.

### TLS certificate not trusted in Windows browser

The mkcert root CA is installed in the WSL Linux trust store only. Windows browsers need the CA imported into the Windows Root store. Run the `certutil` command printed by `setup.sh` in an **elevated** Windows PowerShell:

```powershell
certutil -addstore Root '<path printed by setup.sh>'
```

Then restart your browser. If you no longer have the output, the path is `rootCA.pem` inside the `local-dev/certs/` directory, which you can convert to a Windows path from WSL with:

```bash
wslpath -w local-dev/certs/rootCA.pem
```
