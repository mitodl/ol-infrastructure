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
8. [Backing Up and Restoring Postgres](#backing-up-and-restoring-postgres)
9. [Configuration Reference](#configuration-reference)
10. [Disk Management](#disk-management)
11. [Teardown](#teardown)
12. [Customization & Advanced Setup](#customization--advanced-setup)
13. [Troubleshooting](#troubleshooting)

---

## Overview

This environment runs the MIT Learn application stack as Kubernetes workloads inside a local k3d cluster:

| App | Local URL | Description |
|-----|-----------|-------------|
| mit-learn (frontend) | `https://learn.mit.test` | Next.js frontend |
| mit-learn (backend) | `https://api.learn.mit.test` | Django/granian API |
| learn-ai | `https://ai.learn.mit.test`, `https://api.learn.mit.test/ai/` | Django AI proxy service. The frontend uses the `/ai/` path, which shares mit-learn's login session. |
| learn-ai sandbox | `https://ai-sandbox.learn.mit.test` | Static build of `learn-ai/frontend-demo`, learn-ai checkout only, see [learn-ai sandbox](#learn-ai-sandbox-frontend-demo) |
| mitxonline | `https://mitxonline.mit.test` | MITx Online LMS (Django/granian) |
| Open edX LMS | `https://lms.mit.test` | Courseware, opt-in, see [Open edX](#open-edx); logs in through mitxonline; the learning MFE is at `/learn` |
| Open edX Studio | `https://studio.mit.test` | Course authoring, logs in through the LMS; its home is the authoring MFE at `/authoring` |
| odl-video-service | `https://video.odl.mit.test` | ODL Video Service (Django/uwsgi) |
| micromasters | `https://micromasters.mit.test` | MicroMasters (Django/granian), opt-in, see [MicroMasters](#micromasters) |
| Keycloak SSO | `https://sso.ol.mit.test` | Identity provider (olapps realm) |
| Mailpit | `https://mail.mit.test` | Captured outbound email (web UI) |
| Grafana | `https://grafana.mit.test` | Logs from every service in the cluster (1-week retention) |
| OCW theme dev server | `https://ocw-dev.learn.mit.test` | The ocw-hugo-themes dev server, run on your machine (see below) |

The main app hostnames mirror production with `mit.edu` swapped for `mit.test` (`learn.mit.edu` → `learn.mit.test`, `sso.ol.mit.edu` → `sso.ol.mit.test`), so URLs, CSRF cookies, and OIDC redirect URIs have the same shape as in deployed environments; Open edX, ocw-studio, and the local-only services use local names. `.test` is reserved (RFC 2606) and never resolves on the public internet, so a hostname missing from `/etc/hosts` fails to resolve rather than reaching someone else's server.

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

> **Docker memory:** Allocate at least 12 GB to the Docker VM; 16 GB is comfortable for the full stack (Docker Desktop: Settings → Resources; OrbStack allocates dynamically; Docker Engine on Linux uses host RAM directly — nothing to allocate). Usage scales with how many apps you enable and whether they run from source or prebuilt images: with just mit-learn (from source) and mitxonline enabled, measured pod usage is ~13 GB, the Next.js dev server alone accounting for 3–4 GB. To see what's actually using memory: `kubectl --context local-dev top pods -A | sort -k4 -hr | head`.

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
4. Add all local hostnames to `/etc/hosts` (requires `sudo`)

> **WSL2 users:** If `setup.sh` reports that `/etc/wsl.conf` was updated, run `wsl --shutdown` in Windows PowerShell and reopen your WSL terminal before continuing. The script also prints any Windows hosts entries or a `certutil` command that need to be applied in an elevated Windows PowerShell.

### 2. Configure Tilt

```bash
cp tilt_config.json.example tilt_config.json
# Edit tilt_config.json — see Configuration Reference below
```

At minimum, review `enabled_apps` to enable only the services you need.

For per-developer app env vars and secrets (API keys, feature flags), don't edit the tracked manifests — drop a gitignored `app-env.local.yaml` ConfigMap next to the app's tracked one instead (for mitxonline, its gitignored stack file). See [Local Configuration Overrides](#local-configuration-overrides).

### 3. Start the environment

```bash
./local-dev/scripts/start.sh
```

This will:
1. Validate that `setup.sh` has been run (cluster exists, kubeconfig configured, certs present)
2. Restart the k3d cluster if it was paused by `stop.sh`, and the image registry if it was stopped
3. Re-bake stale container DNS if the host changed networks (see [Troubleshooting](#image-pulls-and-builds-fail-with-dns-io-timeout-after-changing-networks)) — a no-op when current
4. Heal any wedged kubelet exec/streaming (see [Troubleshooting](#kubectl-exec-fails-with-a-502-wedged-kubelet-streaming)) — a no-op when healthy
5. Sync Python dependencies via `uv`
6. Start Tilt

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
| Stop coding but keep apps running | `Ctrl+C` in the Tilt terminal | Apps stay reachable at their `mit.test` URLs; edits no longer sync until Tilt runs again |
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
    │                            # heal-exec.sh, heal-dns.sh, prune-docker.sh,
    │                            # kc-theme-image.sh
    │                            # (each described in its header)
    ├── keycloak/                # Dockerfile for testing an unreleased ol-keycloakify theme
    ├── cluster/                 # k3d cluster definition + registry retention config
    ├── certs/                   # mkcert output (gitignored)
    ├── infra/                   # Pulumi: the shared core and apps_infra stacks (see EXTENDING.md)
    └── apps/<app>/              # Per-app Tiltfile + k8s manifests, or pulumi/: a program rendering
        │                        # the app's deployed definition (EXTENDING.md, "Which surface?")
        ├── configmaps/
        │   └── app-env.local.yaml   # (optional, gitignored) a YAML app's env overrides — see
        │                            # Local Configuration Overrides below
        └── pulumi/
            └── Pulumi.Dev.yaml      # (gitignored) a Pulumi-rendered app's env and memory
                                     # limits — same section
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

Only the listed apps will be deployed. Shared infrastructure always runs, with two
exceptions: the RustFS object store is deployed only when an app that needs it is
enabled, and the data lake only when `data-platform` is listed (see
[Data lake](#data-lake-gravitino--starrocks)), because both are too heavy to run for
developers who have no use for them. The
Tiltfile forwards `enabled_apps` to both Pulumi stacks and to the app Tiltfiles as
`LOCAL_DEV_ENABLED_APPS` so they can make that call.

Remove an app from `enabled_apps` while Tilt is running and Tilt deletes its
workloads when it reloads. A later `tilt up` cleans up only what it applied
itself, so an app removed while Tilt was stopped keeps running: start Tilt
before editing, or delete the workloads yourself, which for GitLab is
`kubectl --context local-dev -n gitlab delete statefulset gitlab`. Going
through `stop.sh` is fine too, since its `tilt down` removes every app still
listed at that point. Worth knowing for GitLab in particular, which holds
~2.6 GiB, though none of this is specific to it.

### Open edX

Open edX runs **in this cluster**, composed from the [lehrer](https://github.com/mitodl/lehrer) repo — not as a second k3d cluster alongside it. It is off by default because the platform image build is long:

```bash
tilt up --context local-dev -- --enabled_apps mit-learn,openedx
```

List the apps comma-separated or repeat `--enabled_apps` once per app. A running Tilt takes the same flags from `tilt args -- --enabled_apps mit-learn,openedx`, which replaces its whole list.

It needs lehrer checked out next to this repo (or `MITOL_WORKSPACE_ROOT` pointing at the directory that holds it), plus the tools lehrer's builds use: run `uv sync && uv run lehrer dev check` in the lehrer checkout, which checks each of them against the version lehrer requires. Nothing else: the `openedx-secrets` Secret is created for you from lehrer's `local-dev/secret-defaults.yaml`, the same file `lehrer dev setup` reads.

How the split works, and why it is not a fork of lehrer's manifests:

| Owned by lehrer | Owned by this repo |
|---|---|
| Platform/notes/codejail images, all k8s manifests, MariaDB + MongoDB | Which shared services to reuse, ingress, hostnames |

`local-dev/apps/openedx/Tiltfile` calls lehrer's `setup()` with this cluster's topology. Valkey and OpenSearch come from the shared `local-infra` namespace rather than a second copy in `openedx` (`manage_infra: False`); MariaDB and MongoDB stay lehrer's, since this cluster has neither. Ingress is APISIX on the shared wildcard cert.

Config differences live in `local-dev/apps/openedx/configmaps/` as `*-config-overrides` ConfigMaps, which lehrer's manifests layer **last** in `envFrom`. Only the keys that actually differ belong there — hosts and public URLs. Everything else is inherited, so a key added on lehrer's side arrives without an edit here. Overriding one more key means adding one line, not copying a ConfigMap, with one constraint: an override reaches Django only if lehrer's settings model declares that key. Check with `kubectl -n openedx exec deploy/lms -- python -c 'import django; django.setup(); from django.conf import settings; print(settings.KEY)'` before trusting a new override.

Two MFEs that lehrer's local-dev does not build are built here through lehrer's dagger module and served under the platform hosts, as in production: the learning MFE at `https://lms.mit.test/learn` and the course-authoring MFE at `https://studio.mit.test/authoring`. Studio depends on the second one: on the master release its home page is a redirect into the authoring MFE and every Studio page links to it, so without it Studio has no working page. Each build is a `dagger call mfe build-legacy-configured` run of a minute or two. The MFE source is cloned inside dagger, so Tilt has nothing local to watch for it; trigger `mfe-learning` or `mfe-authoring` by hand in the Tilt UI to pick up a new upstream commit.

To run Open edX on its own instead — no MIT Learn, no shared infra — use `lehrer dev setup` && `lehrer dev start` in the lehrer repo, which brings up a self-contained cluster.

### Test accounts

Log in at any app (or `https://sso.ol.mit.test` directly) with the seeded Keycloak users: `admin@ol.test`, `student@ol.test`, `prof@ol.test` — password `localdev123` for all three.  <!-- pragma: allowlist secret -->

odl-video-service derives Django permissions from Keycloak group membership: `admin@ol.test` is in the realm's `Admin` group, so it becomes a Django superuser on first login; the other two are plain users.

### Editing code

With an app repo checked out next to `ol-infrastructure`, Tilt live-syncs your edits into the running containers — no rebuild. (Curious how? See [Two transports](ARCHITECTURE.md#two-transports-how-your-code-reaches-a-pod) in ARCHITECTURE.md.) What to expect:

- **Django apps:** granian runs with `--reload` and restarts its workers on each change — the new code serves once Django finishes re-importing (roughly 10–30 s depending on the app). Watch for `Changes detected, reloading workers..` in the app logs. Celery workers and beat don't auto-reload — restart those resources from the Tilt UI after changing task code. When `pyproject.toml` or `uv.lock` changes, Tilt rebuilds the image and rolls the pods, so web, Celery and beat all run the new packages.
- **mitxonline frontends:** the bundles are compiled in the image, so an edit under `frontend/` or to the root yarn files rebuilds the image (about two minutes, including the pre-deploy Job, which re-runs `migrate`) and rolls the pods. The edit is live once the new pods are up.
- **Next.js frontend:** Tilt builds the `local-dev` stage of `Dockerfile.web`, which runs `next dev` (Turbopack). Changes under `frontends/` hot-reload in roughly a second (the first request to each page after a pod start pays a one-time on-demand compile). HMR websockets are proxied through apisix, so the browser hot-updates on `https://learn.mit.test` too. `NEXT_PUBLIC_*` values are runtime env vars (see its `configmaps/app-env.yaml`), not build args, so changing them needs no rebuild. When `yarn.lock` changes, Tilt runs `yarn install` inside the container.
- **No checkout:** Tilt deploys the pre-built Docker Hub image (`mitodl/<app>-app:latest`). To run a specific release instead, add `"<app>=<tag>"` to `prebuilt_tags` in `tilt_config.json` (the Next.js frontend's key is `mit-learn-nextjs`). It works, but does not hot-reload — you can work on mit-learn without having learn-ai checked out.

### Access logs

For a single pod you already have in mind, `kubectl` is the shortest path. Every command in this README passes `--context local-dev`: a bare `kubectl` goes to your kubeconfig's current context, which `eks.py setup` can leave pointed at a deployed cluster.

```bash
# Web pod
kubectl --context local-dev logs -n mit-learn deploy/mitlearn-webapp -c app -f

# Celery worker
kubectl --context local-dev logs -n mit-learn deploy/mitlearn-worker-default -f

# APISIX ingress
kubectl --context local-dev logs -n operations deploy/apisix -f
```

To search across services — or to follow a request from the ingress into the app and on into the worker that picked up the task — use **Grafana at [https://grafana.mit.test](https://grafana.mit.test)**. It opens straight into the UI with no login. Every pod in the cluster is collected, including the ones Tilt does not manage (Postgres, Valkey, OpenSearch, Keycloak, APISIX, Mailpit).

The quickest way in is **Drilldown → Logs**, which needs no LogQL at all: pick a service, then narrow by detected label, field, or log level from the sidebar. Its **Patterns** tab groups repetitive lines into templates, which is how you spot one error buried in a wall of health checks.

For anything the point-and-click path cannot express, start from the pre-provisioned **local-dev logs** dashboard, or go to **Explore → Loki** and write LogQL directly:

```logql
{namespace="mit-learn"}                      # everything in one app's namespace
{app="mitlearn-webapp"} |= "ERROR"           # one workload, filtered
{namespace="operations", app="apisix"}       # ingress access logs
{container="celery"} |= "Traceback"          # across every app's workers
```

Available labels are `namespace`, `pod`, `container`, and `app`, plus `service_name`, which Loki derives on its own and which is what Drilldown's service list is keyed on. Logs are kept for one week by default — see [Log retention](#log-retention) to change that, and `observability_enabled` in [Pulumi stack config](#pulumi-stack-config) to turn the whole stack off.

### Run management commands

```bash
kubectl --context local-dev exec -it -n mit-learn deploy/mitlearn-webapp -c app -- python manage.py shell
kubectl --context local-dev exec -it -n learn-ai deploy/learnai-webapp -c app -- python manage.py dbshell
```

### Run tests

Python tests need the app's environment (database, settings), so run them inside the container — they run against the code Tilt has live-synced, so they see your latest edits:

```bash
kubectl --context local-dev exec -it -n mit-learn deploy/mitlearn-webapp -c app -- pytest main/envs_test.py
```

Frontend (jest) tests don't need the cluster at all — run them from your mit-learn checkout on the host, if you have its JS toolchain set up there (node + corepack + `yarn install`):

```bash
cd ../mit-learn && yarn test useToggle   # the argument is a jest pattern
```

No host toolchain? The dev container has everything installed — run them there instead:

```bash
kubectl --context local-dev exec -it -n mit-learn deploy/mitlearn-nextjs -- sh -c 'cd /app && yarn test useToggle'
```

### Connect to PostgreSQL directly

```bash
kubectl --context local-dev exec -it -n local-infra local-pg-1 -- psql -U app -d mitlearn
```

### Inspect emails (Mailpit)

All outbound email is captured by Mailpit. Access the web UI at `https://mail.mit.test`.

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
| micromasters | `seed-micromasters-superuser` | Create or reset the `admin` / `admin` superuser |
| micromasters | `seed-micromasters-data` | Load seed programs, course runs and learners, then rebuild the search index |
| micromasters | `seed-micromasters-opensearch` | Recreate the OpenSearch indices |

---

## Backing Up and Restoring Postgres

Every PersistentVolume in the cluster lives inside the k3d node containers, so `k3d cluster delete` (what `teardown.sh` does, and what any `k3d-config.yaml` change to the node image needs) destroys all Postgres data. OpenSearch and Qdrant are rebuilt from Postgres by the `seed-mit-learn-opensearch` and `seed-mit-learn-qdrant` seeds; Postgres is the only store worth carrying across.

```bash
# 1. Before teardown: dump every app database to local-dev/.backups/<timestamp>/ (gitignored)
./local-dev/scripts/pg-backup.sh

# 2. Recreate the cluster
./local-dev/scripts/teardown.sh
./local-dev/scripts/setup.sh

# 3. Start Tilt as usual and wait for local-infra-apps and kc-seed-users to go green
./local-dev/scripts/start.sh

# 4. In a second terminal: restore
./local-dev/scripts/pg-restore.sh local-dev/.backups/<timestamp>

# 5. Rebuild the derived stores
tilt trigger seed-mit-learn-opensearch && tilt trigger seed-mit-learn-qdrant
```

`pg-restore.sh` prints its plan, asks for confirmation (`--yes` to skip the prompt), imports `keycloak-users.json` into the recreated realm, then replaces each database from its dump. The apps can stay running: each database is recreated with a connection limit that keeps the `app` role out until its archive has loaded, so an app pod that starts mid-restore is refused rather than allowed to run `migrate` against a half-loaded schema, and pods reconnect on their own once the limit lifts. If the dump predates your checkout's migrations, restart the app Deployments afterwards so `migrate` runs against the restored schema. `pg-backup.sh` records the realm's users alongside the database dumps; `pg-restore.sh` re-imports them under their original ids, so `users_user.global_id` in the restored data stays valid and hand-created users, passwords, and the admin role survive. A user that already exists in the new realm under the same email (the `kc-seed-users` trio, most often) is replaced by the backed-up one, keeping its old id and password.

The `keycloak` database is deliberately never restored — Pulumi owns the realm, and restoring the old database underneath it is what causes the `404 Realm not found` failure described under [Teardown](#teardown). Both scripts run entirely through `kubectl exec` into the Postgres pod, so no Postgres client tools are needed on the host.

---

## Configuration Reference

`tilt_config.json` is your copy of [`tilt_config.json.example`](../tilt_config.json.example) — the example file is the canonical starting point. Keys:

| Key | Default | Description |
|-----|---------|-------------|
| `enabled_apps` | the four original apps | Apps to deploy. Omit any to skip it entirely. `ocw-studio` is **not** in the default and must be added explicitly — see [OCW Studio](#ocw-studio). Neither is `micromasters` — see [MicroMasters](#micromasters). Nor `gitlab` — see [GitLab](#gitlab). `data-platform` is not an app but switches on the [data lake](#data-lake-gravitino--starrocks). |
| `prebuilt_tags` | `[]` (run `latest`) | `["app=tag"]` list of published image tags to run in place of `latest` when the app repo is not checked out locally. An entry for an app that has a checkout is ignored. Entries were not applied before this setting was wired up, so a `tilt_config.json` copied from an older example file carries release tags from early 2026: clear the list unless you want those releases. |
| `lake_host_ports` | `starrocks=9030`, `gravitino=9001`, `rustfs=9000` | `["service=port"]` list moving the host side of a [data lake](#data-lake-gravitino--starrocks) forward off a port something else on your machine holds. |
| `disk_keep_tags`, `disk_buildcache_max_gb` | `3`, 10% of disk | Disk retention knobs — see [Disk Management](#disk-management). |
| `log_retention_period` | `168h` | How long Grafana/Loki keeps logs — see [Log retention](#log-retention). |
| `keycloak_image` | published `mitodl/keycloak` digest | Keycloak server image for the core stack — see [Testing a local ol-keycloakify build](#testing-a-local-ol-keycloakify-build). |
| `per_app_databases` | — | Declared but not wired to anything yet; setting it has no effect. |

The rule of thumb for which config surface a knob belongs to: settings that change **which/how Tilt runs things** (apps, image tags) go in `tilt_config.json`; anything that sets an **env var or secret value inside a workload** (API keys, feature flags, endpoints) goes in a gitignored `app-env.local.yaml` override ConfigMap — see [Local Configuration Overrides](#local-configuration-overrides). mitxonline, whose workloads a Pulumi program renders, takes both its env and its memory limits from its gitignored stack file instead (same section).

### Root domain

Every service hostname derives from the `LOCAL_DEV_ROOT_DOMAIN` environment variable, which defaults to `mit.test`: `learn.<root_domain>`, `api.learn.<root_domain>`, `sso.ol.<root_domain>`, and so on. Set it in your shell environment so that it is exported to `tilt up` and `setup.sh`.

`tilt up` fails immediately if `sso.ol.<root_domain>` does not resolve, whether through DNS or `/etc/hosts`.

After changing the value, or pulling a change to its default, re-run `./local-dev/scripts/setup.sh` to reissue the TLS certificate and rewrite the `/etc/hosts` block; pass `--skip-hosts` to reissue the certificate only, which is what you want when the hostnames already resolve through DNS. The certificate's SANs cover one root domain, so requests to hostnames outside it fail at the TLS layer. Tilt rewrites only the current default (`mit.test`) in app manifests, so a hostname under any other domain in a gitignored `app-env.local.yaml`, or in mitxonline's `mitxonline:vars`, passes through unchanged; check those files for the old domain after a change. Hostnames that apps stored in their databases keep the old domain until rewritten: re-run `seed-mit-learn-mitxonline` for Learn's resource URLs and `seed-ocw-studio-theme-pipeline` for ocw-studio's theme-assets pipeline, and edit the redirect URIs of any OAuth application you registered by hand.

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
LOCAL_DEV_LOG_RETENTION=72h LOCAL_DEV_ENABLED_APPS="${LOCAL_DEV_ENABLED_APPS:-}" \
  pulumi up --stack local-dev.core.Dev
```

`LOCAL_DEV_ENABLED_APPS` has to be set on any hand-run of this stack, listing
the same apps as your `enabled_apps`. The stack refuses to run without it
rather than assume none: assuming none would delete the resources of every
optional app you have enabled. Set it to an empty value if you run none.

Loki only honours a retention window that is a **whole number of days**, so give it hours in multiples of 24 (`48h`, `168h`) or days (`3d`, `7d`). Anything else fails the deploy with an explanatory error rather than being silently ignored.

This knob is deliberately *not* pinned in `Pulumi.local-dev.core.Dev.yaml` — Pulumi config takes precedence over the environment, so a value committed there would override every developer's `tilt_config.json`. Changing it replaces the Loki ConfigMap and restarts Loki; already-ingested logs are re-evaluated against the new window on the next compaction pass (within ~15 minutes).

### Pulumi stack config

The infrastructure is split across two Pulumi stacks:

**`local-dev/infra/core/Pulumi.local-dev.core.Dev.yaml`** — operators, Keycloak, APISIX, database, cache. Chart, operator and image versions are not stack config: they are read from `src/bridge/lib/versions.py`. Components that also run deployed (APISIX, cert-manager, the Keycloak operator, Tika) use the deployed pins; the rest have local-only constants there.

| Key | Default | Description |
|-----|---------|-------------|
| `keycloak_hostname` | `sso.ol.mit.test` | Keycloak ingress hostname |
| `tls_cert_path` | `local-dev/certs/local-dev.pem` | mkcert cert (relative to repo root) |
| `keycloak_image` | published `mitodl/keycloak` digest | Keycloak server image. Leave unset here and use `tilt_config.json` instead — see [Testing a local ol-keycloakify build](#testing-a-local-ol-keycloakify-build). |
| `observability_enabled` | `true` | Deploy Grafana + Loki + Alloy (~2.9GB). Set to `false` on a constrained Docker VM. |

**`local-dev/infra/apps_infra/Pulumi.local-dev.apps-infra.Dev.yaml`** — Keycloak realm and OIDC clients:

| Key | Default | Description |
|-----|---------|-------------|
| `*_client_secret` | `local-dev-*-secret` | OIDC client secrets (change if needed) |

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

Nothing in that table removes a container or a volume, and that is deliberate: every node's data — the k3s datastore and every local-path PersistentVolume, Postgres included — lives in an anonymous Docker volume owned by its node container. **`docker system prune` removes stopped containers**, so a cluster paused by `stop.sh` loses its nodes and their volumes are left dangling: still on disk, but `k3d cluster create` makes fresh ones and will not adopt them. Add `--volumes` and they are deleted outright. `prune-docker.sh` is scoped to images, build cache, and the registry, and never touches either.

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

> **Data:** deleting the cluster deletes every PersistentVolume, Postgres included. Run `./local-dev/scripts/pg-backup.sh` first if there is anything in the databases you want back — see [Backing Up and Restoring Postgres](#backing-up-and-restoring-postgres).

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
cp local-dev/apps/learn-ai/configmaps/app-env.local.yaml.example \
   local-dev/apps/learn-ai/configmaps/app-env.local.yaml
# then add your overrides under data:, e.g.
#   OPENAI_API_KEY: "sk-..."  # pragma: allowlist secret
```

How it works — plain Kubernetes, visible in each app's `deployment.yaml`: every container's `envFrom` list references the override ConfigMap (`learn-ai-env-local` etc.) **last** and with `optional: true`. Kubernetes resolves duplicate `envFrom` keys by letting the last source win, so your overrides beat the tracked ConfigMaps *and* the tracked Secret — secret values are fine in this file, it never leaves your machine. `optional: true` means no file → no ConfigMap → no-op for everyone else.

**mitxonline** has no `deployment.yaml`: a Pulumi program renders its workloads and routes from the deployed definition (`local-dev/apps/mitxonline/pulumi/`). What that changes for overrides:

- Its tracked environment is `LOCAL_ENV_DEFAULTS` in the program, the local counterpart of the deployed program's `DEPLOYED_ENV_DEFAULTS`; the definition's `LOCAL_AND_DEPLOYED_ENV` sits under it.
- Its override file is the stack file, `local-dev/apps/mitxonline/pulumi/Pulumi.Dev.yaml`: gitignored, written on the first apply, with the `.example` next to it listing the keys. Its `mitxonline:vars` map is merged over both into each container's `env:`, so the precedence is the same as for the other apps; because `env:` beats `envFrom`, a key set there also beats the same key in `secrets.yaml`. The keys are the deployed stacks' own: `src/ol_infrastructure/applications/mitxonline/Pulumi.Production.yaml` carries the same `vars` block.
- The same file sets memory limits: `mitxonline:web_memory_limit` (production's key) plus `worker_memory_limit`, `pre_deploy_memory_limit` and `beat_memory_limit`. Requests stay at the program's laptop values, so a limit below the request fails Kubernetes' validation and the apply says so.
- The definition adds `OTEL_RESOURCE_ATTRIBUTES`, the tracked Secret arrives through `envFrom`, and the component adds `PORT`.
- Editing the program, the stack file or `secrets.yaml` re-runs the program and rolls the pods; a `secrets.yaml` edit also re-runs the migrate Job. A `configmaps/app-env.local.yaml` for mitxonline is not read: move its `data:` entries under `mitxonline:vars:` in the stack file and delete it; until then the apply log names its keys.
- The overridden key names and the image in use appear in the `mitxonline-app` resource's log; `kubectl --context local-dev get deploy -n mitxonline mitxonline-app -o yaml` shows the merged result. Override values sit in the pod spec and in the Pulumi state under `local-dev/infra/.pulumi/`, so treat both as sensitive output.
- The pre-deploy Job also runs `configure_wagtail`, so a fresh database gets its Wagtail site and index pages without a seed step (`seed.sh` has no mitxonline default for the same reason; `tilt trigger mitxonline-app` recreates the stack, Job included, so it repeats them). Logins come from Keycloak; no Django superuser is seeded.
- The Job carries the component's `ttlSecondsAfterFinished` of 30 minutes, so `kubectl --context local-dev -n mitxonline logs job/mitxonline-app-pre-deploy` answers NotFound after that, and the next apply, whatever triggered it, re-creates the Job and re-runs migrate (about 25 s) before the pods roll.
- Before switching to `main` or any branch from before the program, run `tilt down` (or `local-dev/scripts/pulumi-app-apply.sh mitxonline down`): Tilt does not delete the Pulumi-owned objects, and the old YAML then serves the same hosts beside them.

Day-to-day behavior:

- Creating or editing the file mid-session re-applies it — no Tilt restart. Pods roll automatically so new values actually take effect: because Kubernetes does not restart pods on ConfigMap/Secret changes, `tiltlib.star` stamps a fingerprint of every applied ConfigMap's and Secret's data onto each Deployment's pod template (the `ol.mit.edu/config-hash` annotation; this is for the apps Tilt applies as YAML — mitxonline's program hashes the Secret onto the pod template itself) — this covers edits to the tracked `app-env.yaml`/`secrets.yaml` too, not just this override file — which is also the idiomatic production pattern (cf. Helm `checksum/config` annotations).
- Overridden key **names** (never values) are printed in the **Tiltfile resource's log** in the Tilt UI, and your full delta is inspectable in-cluster at any time: `kubectl --context local-dev get cm -n learn-ai learn-ai-env-local -o yaml` (mitxonline applies no such ConfigMap; see above)
- Gotchas: for the YAML-applied apps the ConfigMap's `metadata.name` must match what `deployment.yaml` references (`<app>-env-local` — copy the example, don't type it), and all `data:` values must be YAML **strings** (quote things like `"True"` and `"8080"`). A typo'd key is applied but ignored by the app. If you *delete* the file mid-session, prefer emptying `data:` instead — the already-applied ConfigMap can linger in-cluster until `tilt down`.

Every YAML-applied app supports this, including **mit-learn-nextjs** (e.g. per-developer PostHog credentials — see its `app-env.local.yaml.example`); mitxonline's equivalent is its stack file, above.

Scope notes:

- **OpenAI key for LiteLLM** (`local-infra` namespace, Pulumi-managed) is separate from the app overlays — create the Secret directly (the deployment marks it optional, so this is safe to skip entirely):
  ```bash
  kubectl --context local-dev create secret generic litellm-secrets -n local-infra \
    --from-literal=openai_api_key=sk-your-key  # pragma: allowlist secret
  ```
  (That `openai_api_key` Secret *data key* is unrelated to the old `tilt_config.json` key of the same name, which was never wired to anything and has been removed.)
- **learn-ai calls OpenAI directly**, like RC and production, so the LiteLLM key above does not reach it. Set `OPENAI_API_KEY` in learn-ai's `app-env.local.yaml`, plus `LEARN_ACCESS_TOKEN` for the bots' course search (see `apps/learn-ai/configmaps/app-env.local.yaml.example`). To route learn-ai through LiteLLM instead, copy the proxy block from `apps/learn-ai/configmaps/app-env.local.yaml.example` into your `app-env.local.yaml` and uncomment it.

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

### Testing a local ol-keycloakify build

The login, account, and email themes in local Keycloak come from the [ol-keycloakify](https://github.com/mitodl/ol-keycloakify) release baked into the published `mitodl/keycloak` image. To see an unreleased branch of the theme, check it out next to this repo and run:

```bash
./local-dev/scripts/kc-theme-image.sh            # ../ol-keycloakify
./local-dev/scripts/kc-theme-image.sh ~/src/ol-keycloakify   # or any checkout
```

The script builds the theme from the checkout's sources inside Docker (no Node, Yarn, Maven, or JDK needed on the host), bakes the jar into a Keycloak image based on the published one, pushes it to the k3d registry, and writes the image into `keycloak_image` in your gitignored `tilt_config.json`. A running `tilt up` picks that up on its own: `local-infra-core` re-applies and the operator rolls `keycloak-0`, which takes a minute or two and logs you out of every local app. A first build takes about three minutes, mostly downloading the theme's dependencies; later builds reuse them. If the build fails at `yarn install --immutable`, the checkout's `yarn.lock` is out of date: run `yarn install` there and commit it.

Confirm the pod is on your tag, then log in again or trigger a password reset to see the login and email themes:

```bash
kubectl --context local-dev -n local-infra get pod keycloak-0 -o jsonpath='{.spec.containers[0].image}'
```

Emails land in [Mailpit](#inspect-emails-mailpit); the olapps realm enables the `UPDATE_EMAIL` required action, so changing an email from the [account console](https://sso.ol.mit.test/realms/olapps/account/) exercises the email-update confirmation template as well.

Edit, re-run the script, and Keycloak rolls again. The tag is the checkout's short commit plus a hash of its uncommitted changes (tracked edits and untracked files; gitignored files don't count) and of the Dockerfile, so each distinct input gets its own tag and the operator, which only rolls when the image string changes, rolls once per change. The registry keeps the ten most recent tags per repository, so if `keycloak-0` ever fails to pull an old tag after many builds, re-run the script. To return to the published image:

```bash
./local-dev/scripts/kc-theme-image.sh --reset   # sets keycloak_image back to ""
```

The realm, clients, and seeded users live in Postgres and are untouched by the image swap. Under the hood, Tilt forwards `keycloak_image` to the core Pulumi stack as `LOCAL_DEV_KEYCLOAK_IMAGE`, so the same value works for a hand-run `pulumi up`.

Why an image at all: Keycloak runs `--optimized`, and an optimized Keycloak refuses to start when a jar in `providers/` differs from the one it was built against, so the theme cannot be copied into the running pod. The Dockerfile (`local-dev/keycloak/Dockerfile`) starts from the same `mitodl/keycloak` digest the core stack defaults to (the script reads it from `local-dev/infra/core/__main__.py`) and reruns `kc.sh build` with the flags from `Dockerfile.hosted` in [ol-keycloak](https://github.com/mitodl/ol-keycloak); when bumping the digest, check those flags still match.

### S3 Storage (RustFS)

The stack runs [RustFS](https://rustfs.com), an S3-compatible object store, as the
local stand-in for AWS S3. It is the maintained replacement for MinIO, whose community
server is effectively frozen.

It is **not** deployed by default. The core Pulumi stack brings it up only when
`enabled_apps` contains an app listed in `OBJECT_STORE_APPS`
(`local-dev/infra/modules/objectstore.py`), which today means `ocw-studio` or
`data-platform`.

Two addresses, and apps generally need both:

| From | Address |
|------|---------|
| Inside the cluster (boto3, the site-host nginx) | `http://rustfs.local-infra.svc.cluster.local:9000` |
| A browser (media URLs, presigned links) | `https://s3.mit.test` |

Credentials are the fixed local-dev pair `localdevaccess` / `localdevsecret123`,
defined in `objectstore.py` and repeated in each app's `secrets.yaml`.

A bootstrap Job creates the buckets of every enabled app. ocw-studio's are each
granted anonymous read with a bucket policy; the data lake's bucket is left private. The policy matters: RustFS does not implement S3 ACLs, so the canned
`public-read` header boto3 sends is accepted and ignored, and a bucket policy is the
only grant that actually takes effect. Published sites are served with no credentials,
so without it every page would 403.

To add buckets for another app, add it to `OBJECT_STORE_BUCKETS` with an
`AppBuckets` saying which of its buckets are public, private or versioned.

Removing the last such app from `enabled_apps` tears the object store back down on the
next reconcile. The objects survive: the StatefulSet's `data-rustfs-0` PVC is not
garbage collected with it, so re-enabling the app reattaches the same volume and the
buckets are still there. Delete the PVC as well if you actually want the disk back,
and note that this does discard the objects:

```bash
kubectl --context local-dev -n local-infra delete pvc data-rustfs-0
```

**Using an external MinIO instead** — run it on your host and point an app at it via
its gitignored `app-env.local.yaml` (see [Local Configuration Overrides](#local-configuration-overrides)):

All three endpoint settings have to move together, or uploads land in one store while
reads and browser URLs still point at the other:

```yaml
# boto3 and django-storages
AWS_S3_ENDPOINT_URL: "http://172.17.0.1:9000"
# generic botocore clients, and the `aws` CLI inside pipeline tasks
AWS_ENDPOINT_URL: "http://172.17.0.1:9000"
# what the browser is handed for media URLs, so it must be reachable from there
AWS_S3_CUSTOM_DOMAIN: "172.17.0.1:9000/ol-ocw-studio-app-local"
AWS_ACCESS_KEY_ID: "minioadmin"  # pragma: allowlist secret
AWS_SECRET_ACCESS_KEY: "minioadmin"  # pragma: allowlist secret
```

### Data lake (Gravitino + StarRocks)

Add `data-platform` to `enabled_apps` to get a local stand-in for the data lake:

```json
{
  "enabled_apps": ["mit-learn", "data-platform"]
}
```

It is not an app. The core Pulumi stack deploys, all in `local-infra`:

- RustFS with a private `ol-data-lake-local` bucket (see above).
- [Apache Gravitino](https://gravitino.apache.org) as the Iceberg REST catalog, with
  its state in two databases (`gravitino`, `iceberg`) on the shared Postgres cluster.
- StarRocks, the single-pod `allin1` image at the version the deployed clusters run,
  with an external catalog `ol_data_lake_local` pointing at Gravitino and an empty
  `ol_warehouse_local` schema in it for dbt to connect to. It also gets two empty
  databases of its own, `b2b_analytics` and `b2b_learner_records`, for the b2b
  materialized views.

Budget about 2GB of memory, a 3GB image pull and a 20Gi volume for StarRocks.

| What | Address |
|------|---------|
| StarRocks, from your machine | `mysql -h 127.0.0.1 -P 9030 -u root` (Tilt keeps the port-forward open as `starrocks-port-forward`) |
| StarRocks, inside the cluster | `starrocks.local-infra.svc.cluster.local:9030`, user `root`, no password |
| Iceberg REST catalog, inside the cluster | `http://gravitino.local-infra.svc.cluster.local:9001/iceberg` |
| Iceberg REST catalog, from your machine | `http://localhost:9001/iceberg` (`iceberg-catalog-port-forward`) |
| RustFS, from your machine, for an Iceberg writer | `http://localhost:9000` (`lake-object-store-port-forward`) |
| Gravitino management API and UI | `kubectl --context local-dev -n local-infra port-forward svc/gravitino 8090:8090` |

```sql
CREATE DATABASE ol_data_lake_local.scratch;
CREATE TABLE ol_data_lake_local.scratch.t (id int, name string);
INSERT INTO ol_data_lake_local.scratch.t VALUES (1, 'a');
SELECT * FROM ol_data_lake_local.scratch.t;
```

dbt builds onto it from an [ol-data-platform](https://github.com/mitodl/ol-data-platform)
checkout, with no Vault login. Run these from its repository root:

```bash
# Once per checkout: install the `ol-dbt` CLI into .venv.
uv sync

# In each new shell: put it on PATH. Without this, write each `ol-dbt`
# in this README as `uv run ol-dbt`.
source .venv/bin/activate

# Once per checkout: install the dbt packages.
(cd src/ol_dbt && uv run dbt deps)

ol-dbt fixtures load
ol-dbt starrocks build --env dev --select +integrations__learn__ocw_courses
```

`--env dev` is the `starrocks_local` dbt profile: catalog `ol_data_lake_local`, schema
`ol_warehouse_local`, over the port-forward above. Models land in
`ol_warehouse_local_<layer>` (`_staging`, `_intermediate`, `_integrations`, ...), and
the sources read `ol_warehouse_local_raw`. `ol-dbt fixtures load` fills that schema from
the fixture files committed in ol-data-platform, and a model whose raw tables have no
fixture fails on the missing source table. See
[docs/LOCAL_LAKE.md](https://github.com/mitodl/ol-data-platform/blob/main/docs/LOCAL_LAKE.md)
there for the fixture format and how to add one.

A dlt source in that checkout writes raw tables from a local app database into the same
schema, over the catalog and RustFS forwards above plus one to Postgres
(`kubectl --context local-dev -n local-infra port-forward svc/local-pg-rw 5432:5432`):

```bash
cd src/ol_dlt && uv run python -m ol_dlt.sources.keycloak
```

`ol-dbt ingest local`, from the repository root, runs every source the ingestion
inventory marks `strategies.local: ingest` the same way. Today those are Keycloak and MITx
Online, so enable `mitxonline` and run its `seed-mitxonline-course-data` seed first: the
MITx Online source has no courses to load until it has run. `--unit mitxonline/app_postgres`
runs one. See "Raw tables from a local app database" in docs/LOCAL_LAKE.md there.

The three forwards take host ports 9030, 9001 and 9000. An external MinIO on your host
(above) also binds 9000: the RustFS forward then fails in its loop, and the dlt run
talks to the MinIO in its place. Move the forward in `tilt_config.json` and give dlt the
same address:

```json
"lake_host_ports": ["rustfs=9100"]
```

```bash
export PYICEBERG_CATALOG__OL_DATA_LAKE_LOCAL__S3__ENDPOINT=http://localhost:9100
```

A moved Gravitino forward takes `PYICEBERG_CATALOG__OL_DATA_LAKE_LOCAL__URI`. The
`starrocks_local` dbt profile has port 9030 written in, so a moved StarRocks forward
means editing that profile.

The catalog tells every client to use RustFS's in-cluster address, so any other
host-side Iceberg writer has to keep its own endpoint the way `ol_dlt/local_lake.py`
does.

The b2b materialized views are StarRocks tables, not lake tables, so they have a
profile of their own, `starrocks_local_b2b`. It writes `default_catalog.b2b_analytics`
and `default_catalog.b2b_learner_records` and reads
`ol_data_lake_local.ol_warehouse_local_dimensional`, which `ol-dbt fixtures
load-dimensional b2b_organization` fills (see [ol-analytics-api](#ol-analytics-api)). A bare
`dagster dev` in the lakehouse code location builds on it, and so does:

```bash
ol-dbt starrocks build --env dev --target starrocks_local_b2b --select tag:starrocks
```

MIT Learn's warehouse tasks are pointed at the local lake with no further setup. With
both `mit-learn` and `data-platform` enabled, its pods get `STARROCKS_HOST`,
`STARROCKS_USER`, `WAREHOUSE_CATALOG` and `WAREHOUSE_SCHEMA` from
`local-dev/apps/mit-learn/configmaps/warehouse-env.yaml`. Build the
`integrations__learn__*` tables a task reads before running it: the one its
`table_name` names, and any other its `fetch_and_upsert` reads (the xPRO course task
also reads the runs table and refuses to run when either is empty).

From a fixture row to a course in the local MIT Learn, with xPRO as the example. The
first two commands run from the ol-data-platform repository root, the rest here:

```bash
ol-dbt fixtures load
ol-dbt starrocks build --env dev --select \
  "+integrations__learn__xpro_courses +integrations__learn__xpro_runs +integrations__learn__xpro_programs"

kubectl --context local-dev -n mit-learn exec -i deploy/mitlearn-webapp -c app -- python manage.py shell <<'PY'
from learning_resources.models import ETLSourceOwnership
from learning_resources.tasks import SyncXproCoursesTask, SyncXproProgramsTask

for resource_type in ("course", "program"):
    ETLSourceOwnership.objects.update_or_create(
        etl_source="xpro", resource_type=resource_type, defaults={"owner": "warehouse"}
    )
print(SyncXproCoursesTask.run(full_refresh=True))
print(SyncXproProgramsTask.run(full_refresh=True))
PY
kubectl --context local-dev -n mit-learn exec deploy/mitlearn-webapp -c app -- python manage.py clear_cache

curl -sk "https://api.learn.mit.test/api/v1/courses/?platform=xpro"
```

- The xPRO tasks landed on MIT Learn's main branch in
  [mit-learn #4054](https://github.com/mitodl/mit-learn/pull/4054). Tilt builds the
  image from the sibling `mit-learn` directory, so a checkout older than that has no
  `SyncXproCoursesTask`. After pulling, the webapp pod picks the change up and the
  Celery workers do not until they are restarted, which does not matter for a task run
  from the shell as here.
- A task writes nothing until `ETLSourceOwnership` names the warehouse for what it
  loads. Without the rows it logs `Skipping warehouse write for xpro` and returns 0. See
  `docs/how-to/etl-source-cutover.md` in mit-learn.
- Celery beat is off locally, so run the task by hand. The task modules bind each name
  to a registered task instance, so call `run` on it directly.
- The list endpoints are cached for a day and the beat task that clears the cache does
  not run, so a second sync is not visible through the API until `clear_cache`.

To see a change, edit the fixture or the model, then repeat the load (for a fixture
edit), the build, the task and `clear_cache`. An edit that takes a published course or
program away makes the next sync fail before it writes: the tasks refuse to unpublish
more than 10% of a source's resources, which a handful of fixture rows exceeds with one
removal. The cutover document above covers `allow_mass_unpublish`.

Things that will trip you up:

- **A Postgres cluster created before 2026-07-08 needs one grant first.** The
  `lakehouse-databases` Job creates its databases as the `app` role, and `CREATEDB` is
  only granted when the cluster is first initialized. If the Job fails with
  `permission denied to create database`, run this once and re-run `local-infra-core`:

  ```bash
  kubectl --context local-dev -n local-infra exec local-pg-1 -- psql -U postgres -c 'ALTER ROLE app CREATEDB'
  ```

- **A plain pyiceberg client on your machine cannot write to the lake.** The catalog
  tells every client to use the in-cluster RustFS address, overriding whatever endpoint
  the client was given, so the client creates the table and then fails the data write
  with `Could not resolve host: rustfs.local-infra.svc.cluster.local`. The dlt sources
  in ol-data-platform do write from the host: `ol_dlt/local_lake.py` keeps the
  forwarded endpoint.
- **pyiceberg needs HTTP Basic auth configured**: an `auth` property of type `basic`
  with any username and any non-empty password. The username becomes the Gravitino
  principal and the password is not checked. Gravitino accepts a request with no
  `Authorization` header and refuses a Bearer one; pyiceberg 0.12.0 with no auth
  configured got a 401.
- **Most changed catalog properties do not reach an existing catalog.** The
  `lakehouse-catalogs` Job only creates what is missing. Drop the catalog (in StarRocks,
  in Gravitino, or both), delete the Job (`kubectl --context local-dev -n local-infra delete job
  lakehouse-catalogs`; it also expires by itself after ten minutes) and re-run
  `local-infra-core`. The exception is `STARROCKS_CATALOG_SETTINGS` in
  `infra/modules/lakehouse.py`: the Job sets those on the StarRocks catalog with `ALTER
  CATALOG` every time it runs, so a change there needs only the Job deleted and
  `local-infra-core` re-run.

This matches the deployed lake in engine, engine version and catalog protocol, and
differs from it in ways that can hide a problem:

| | Local | QA / Production |
|---|---|---|
| Catalog authentication | none (Gravitino `simple` authenticator, plain HTTP) | Keycloak JWT per user, HTTPS |
| Catalog authorization | off | grants per governance role |
| Iceberg views | work (JDBC catalog backend) | not available (Glue backend), so a view-materialized dbt model builds locally and fails in QA |
| S3 credentials | the RustFS root key, static | vended per table from an IAM role |
| StarRocks storage | shared-nothing, internal tables on the pod's volume | shared-data, internal tables on S3 |

Removing `data-platform` from `enabled_apps` tears all of it down on the next
reconcile. The `data-starrocks-0` and `data-rustfs-0` PVCs and the two Postgres
databases are left behind, so re-enabling it picks the lake back up. To start clean
(leave `data-rustfs-0` alone if ocw-studio is enabled, it holds those buckets too):

```bash
kubectl --context local-dev -n local-infra delete pvc data-starrocks-0 data-rustfs-0
kubectl --context local-dev -n local-infra exec local-pg-1 -- psql -U postgres -c 'DROP DATABASE gravitino WITH (FORCE)' -c 'DROP DATABASE iceberg WITH (FORCE)'
```

#### Rehearsing a webhook cutover

The other way a source reaches MIT Learn is the data platform's webhook push: a Dagster
delivery asset reads an `integrations__learn__*` table and posts it to
`/api/v1/webhooks/learning_resources/`. With `DBT_MODEL_LOCAL_LAKE=1` the asset reads
the table your local build made, so the whole flip in mit-learn's
`docs/how-to/etl-source-cutover.md` (legacy load, comparison, flip, first delivery, roll
back) can be run here before it is run anywhere that has a catalog to lose.

Open Learning Library is the source to rehearse with. Its legacy pipeline and its dlt
source read the same CSV, so both sides load real rows with no credentials. The baseline:

```bash
kubectl --context local-dev -n mit-learn exec -i deploy/mitlearn-webapp -c app -- python manage.py shell <<'PY'
from learning_resources.etl import pipelines
from learning_resources.models import LearningResource

print(len(pipelines.oll_etl()))
print(*sorted(LearningResource.objects.filter(
    etl_source="oll", resource_type="course", published=True
).values_list("readable_id", flat=True)), sep="\n")
PY
```

What the webhook would deliver, from the ol-data-platform repository root:

```bash
(cd src/ol_dlt && uv run python -m ol_dlt.sources.oll)
ol-dbt starrocks build --env dev --select +integrations__learn__oll_courses

mysql -h 127.0.0.1 -P 9030 -u root -N -e "SELECT readable_id FROM
  ol_data_lake_local.ol_warehouse_local_integrations.integrations__learn__oll_courses ORDER BY 1"
```

Every id in the first list and not in the second is unpublished by the first delivery.
The flip and the delivery:

```bash
kubectl --context local-dev -n mit-learn exec -i deploy/mitlearn-webapp -c app -- python manage.py shell <<'PY'
from learning_resources.models import ETLSourceOwnership

ETLSourceOwnership.objects.update_or_create(
    etl_source="oll", resource_type="course", defaults={"owner": "webhook"}
)
PY

cd dg_projects/delivery   # in ol-data-platform
unset DBT_MODEL_FIXTURE_DIR
export VAULT_OIDC_NONINTERACTIVE=1 DBT_MODEL_LOCAL_LAKE=1
export MIT_LEARN_BASE_URL=https://api.learn.mit.test
export MIT_LEARN_WEBHOOK_SECRET=local-dev-mitlearn-insecure-webhook-secret  # pragma: allowlist secret
uv run dagster asset materialize -m delivery.definitions --select "mit_learn_delivery/oll_webhook"

curl -sk "https://api.learn.mit.test/api/v1/courses/?platform=oll&limit=1" | jq .count
```

To roll back, give the pair back to the legacy pipeline, run it and clear the cache:

```bash
kubectl --context local-dev -n mit-learn exec -i deploy/mitlearn-webapp -c app -- python manage.py shell <<'PY'
from learning_resources.etl import pipelines
from learning_resources.models import ETLSourceOwnership

ETLSourceOwnership.objects.filter(etl_source="oll", resource_type="course").update(owner="legacy")
print(len(pipelines.oll_etl()))
PY
kubectl --context local-dev -n mit-learn exec deploy/mitlearn-webapp -c app -- python manage.py clear_cache
```

- Before the flip the delivery fails with `409` on every attempt of its retry policy and
  MIT Learn logs `learning_resources webhook rejected`. After it, MIT Learn logs
  `learning_resources webhook processed` with the count loaded, and `oll_etl()` returns
  an empty list and logs `Skipping legacy write for oll`.
- The webhook clears the API cache itself. The legacy pipeline run from a shell does not,
  which is why the roll back needs `clear_cache`.
- A delivery is a full sync with no limit on what it unpublishes. MIT Learn's
  `learning_resources` warehouse tasks refuse to unpublish more than 10% of a source,
  and the webhook has no such check: a model edit that left 14 of 58 rows unpublished
  the other 44 in one run. A model that builds with no rows at all gets a `400` from
  this asset and changes nothing.
- As of 2026-10-11 the comparison is not empty for this source: the legacy pipeline
  publishes 63 courses and the model has 58. The sheet marks six rows as offered by
  OCW. The staging model drops all six, and the legacy pipeline skips one and loads
  the other five under a `readable_id` of another form.

### OCW Studio

OCW Studio is the CMS behind OCW: authors edit course content in it, and publishing
commits that content to git and hands Concourse a pipeline that builds the site and
writes the result into an S3 bucket. Reproducing that locally needs more moving parts
than the other apps, which is why it is opt-in:

```json
{
  "enabled_apps": ["mit-learn", "ocw-studio"]
}
```

Enabling it deploys the object store described above, a Concourse install (web plus a
privileged worker), the Studio app and its celery worker, and an nginx deployment that
serves published sites out of the buckets.

| Hostname | What it serves |
|----------|----------------|
| `studio.ocw.mit.test` | Studio itself |
| `draft.ocw.mit.test` | the preview bucket — what "Publish draft" writes |
| `live.ocw.mit.test` | the publish bucket — what "Publish live" writes |
| `test.ocw.mit.test` | the test bucket the pipeline's smoke-test step reads |
| `concourse.ocw.mit.test` | the Concourse UI (log in as `test` / `test`) |

Re-run `./local-dev/scripts/setup.sh` after enabling it: those hostnames need
`/etc/hosts` entries and the TLS cert needs a `*.ocw.<domain>` wildcard, neither of
which an existing setup has.

**Before publishing anything**, run the `seed-ocw-studio-*` resources from the Tilt UI,
and give Studio a git host it can push to. There is no usable default: publishing
commits site content to a real repo, so set `GIT_ORGANIZATION` and `GIT_TOKEN` in
`local-dev/apps/ocw-studio/configmaps/app-env.local.yaml` (the tracked
`app-env.local.yaml.example` has the shape for both GitHub and GitLab). To work on
Studio without publishing at all, set `CONTENT_SYNC_PIPELINE_BACKEND: ""` there —
editing then works and Concourse is never contacted.

What differs from the other apps:

- **Frontend changes need an image rebuild.** There is no webpack dev server: it needs
  node, and the app image is Python-only, so there is nothing for Tilt to live-sync JS
  into. Django reads the bundles baked in by the Dockerfile's `node_builder` stage,
  which is why this app builds the `local-dev` target, the `production` target plus
  dev dependencies. Tilt rebuilds automatically when JS or SCSS changes; it is just
  slower than a hot reload.
- **Python changes reload like the other Django apps, but only with a checkout.** The
  `local-dev` target turns granian's reload on through `GRANIAN_RELOAD`, so Tilt
  syncs edits under the app's Python directories and granian restarts its worker
  (`Changes detected, reloading workers..` in the logs). Without an ocw-studio
  checkout, Tilt runs the prebuilt production image, which does not reload; restart
  `ocwstudio-webapp` from the Tilt UI instead.

### MicroMasters

MicroMasters is opt-in. It is here so the data platform's `micromasters` ingestion has
a local Postgres database to read, and most developers do not need it running:

```json
{
  "enabled_apps": ["mit-learn", "micromasters"]
}
```

Enabling it creates the `micromasters` database on the shared Postgres cluster and
deploys the web app at `https://micromasters.mit.test` plus one celery worker. Re-run
`./local-dev/scripts/setup.sh` first if your `/etc/hosts` predates the app; the
existing `*.<domain>` certificate already covers the hostname.

Run `seed-micromasters-superuser` and then `seed-micromasters-data` from the Tilt UI.
The first gives you a login, the second fills the database with 2 programs and 120
learners and rebuilds the search index.

What differs from the other apps:

- **Log in at `/admin/` as `admin` / `admin`.** Learner sign-in is OAuth against edX
  or MITx Online, and the tracked config has credentials for neither.
  `local-dev/apps/micromasters/configmaps/app-env.local.yaml.example` lists the
  settings to fill in if you need it.
- **Every code change rebuilds the image.** The image has no `watchfiles`, so granian
  cannot reload, and the frontend bundles are baked in by the Dockerfile's
  `node_builder` stage. Tilt rebuilds automatically when the sibling `micromasters`
  checkout changes.
- **Nothing scheduled runs.** There is no celery beat deployment, so the edX and MIT
  Learn syncs never fire.

### ol-analytics-api

[ol-analytics-api](https://github.com/mitodl/ol-analytics-api) serves MIT Learn's org
analytics dashboard (`/dashboard/organization/<org>/analytics`) out of StarRocks. It is
opt-in, and it needs the lake and MITx Online alongside it; the Tiltfile refuses to load
it without them:

```json
{
  "enabled_apps": ["mit-learn", "mitxonline", "data-platform", "ol-analytics-api"]
}
```

Re-run `./local-dev/scripts/setup.sh` first if your `/etc/hosts` predates the app. Tilt
builds the image from a sibling `ol-analytics-api` checkout and syncs `src/` into the
pod, where granian reloads on change; a `pyproject.toml` or `uv.lock` edit rebuilds the
image.

It is wired the way the deployed Learn-scoped host is:

- `https://analytics.learn.mit.test` routes to it through an openid-connect plugin that
  reads mit-learn's session cookie and never redirects, so a browser logged in to MIT
  Learn reaches it with `X-Userinfo` and an anonymous one gets a JSON 401.
- The Next.js frontend gets `NEXT_PUBLIC_ANALYTICS_API_BASE_URL` only while the app is
  enabled (`apps/mit-learn-nextjs/configmaps/analytics-env.yaml`). Without it the
  dashboard says analytics is unavailable.
- The app reads the lake's StarRocks as `root`, from the `b2b_analytics` and
  `b2b_learner_records` databases the lake creates empty.
- The org-manager check calls the local MITx Online at `https://api.mitxonline.mit.test`
  with a client-credentials token. MITx Online's pre-deploy Job registers that
  Application while `ol-analytics-api` is in `enabled_apps`
  (`ANALYTICS_API_OAUTH_APP_SCRIPT` in `apps/mitxonline/pulumi/inputs.py`), so enabling
  the app re-applies mitxonline once.
- MIT Learn's login requests `organization:*`, and `kc-fix-org-claim` sets the realm's
  organization mapper to add each organization's id, which the deployed realms do. Log
  out and back in after either reaches your cluster: the claim is fixed when the
  session is created.

A request passes only when all of these agree on one organization:

- the logged-in user is a member of a Keycloak organization in the `olapps` realm;
- MITx Online has that organization's page, with `sso_organization_id` set to the
  Keycloak organization's id, and a `UserOrganization` row marking the user as a
  manager (deployed, `is_manager` is curated in the Django admin);
- the StarRocks views have rows for that `sso_organization_id`.

Keycloak assigns organization ids itself (its admin API ignores an id in the request),
so the StarRocks rows come from a fixture in ol-data-platform that takes the ids when it
is loaded. To go from an empty stack to a populated dashboard, using the realm's
`local-dev` organization:

1. In the Keycloak admin console (`https://sso.ol.mit.test/admin/`, `admin` / `admin`),
   add your test user to the `Local Dev` organization in the `olapps` realm.
2. Give MITx Online the organization and a contract, then log in to MIT Learn as that
   user and make them a manager:

   ```bash
   kubectl --context local-dev -n mitxonline exec deploy/mitxonline-app -- python manage.py sync_keycloak_orgs
   kubectl --context local-dev -n mitxonline exec deploy/mitxonline-app -- \
       python manage.py b2b_contract create "Local Dev" "Local Dev contract" managed
   kubectl --context local-dev -n mitxonline exec -i deploy/mitxonline-app -- python manage.py shell <<'PY'
   from b2b.models import ContractPage, UserOrganization

   UserOrganization.objects.filter(organization__name="Local Dev").update(is_manager=True)
   for contract in ContractPage.objects.filter(organization__name="Local Dev"):
       print(contract.organization.sso_organization_id, contract.id, contract.title)
   PY
   ```

3. From an ol-data-platform checkout, load the fixture with the two ids that printed,
   build the views and refresh them. "B2B views" in
   [docs/LOCAL_LAKE.md](https://github.com/mitodl/ol-data-platform/blob/main/docs/LOCAL_LAKE.md)
   has the commands.

The `UserOrganization` row only exists once MITx Online has seen the organization in the
user's login, so step 2's update matches nothing if it runs before that login.

The dashboard is behind the `b2b-analytics-dashboard` PostHog flag, and its per-learner
pages also need `b2b-learner-analytics`. Enabling the app sets both as
`NEXT_PUBLIC_FEATURE_*` on the frontend, but that only bootstraps them, and today the
pages wait for PostHog's server to answer before they render, so the bootstrapped values
alone do not open them (https://github.com/mitodl/mit-learn/pull/4189 changes that). Set
`NEXT_PUBLIC_POSTHOG_API_KEY` (and `_API_HOST`) in
`apps/mit-learn-nextjs/configmaps/app-env.local.yaml` and turn the flags on for yourself,
e.g. with an override from the PostHog toolbar.

### GitLab

Off by default. Add `gitlab` to `enabled_apps` to get a GitLab CE instance at
`https://gitlab.<root_domain>`, so ocw-studio's GitLab content-sync backend has
somewhere to push without an external server.

**It is the heaviest thing in this repo.** Read the resource numbers below
before switching it on, particularly if you already run several apps.

Sign in as `root` with the password in `local-dev/apps/gitlab/secrets.yaml`.

Publishing from ocw-studio to this instance works because Concourse's resource
containers are given the local mkcert CA (`CONCOURSE_CERTS_DIR` in
`local-dev/apps/ocw-studio/concourse.yaml`). Without it the git resource
cannot verify a certificate issued for `*.<root domain>` and every clone
fails. Note that this covers resource containers only, which is what checks
and fetches use; task containers still reach local services over plain HTTP by
design.

A personal access token is seeded for you by the `gitlab-token` Tilt resource,
with the fixed value also in that file, so an app can be configured against
this instance before anyone has logged into it. Re-running that resource is
safe; it updates the existing token rather than adding another.

To point ocw-studio at it, create a group in the GitLab UI to push into, then
uncomment the in-cluster block in
`local-dev/apps/ocw-studio/configmaps/app-env.local.yaml.example` and copy it
into your `app-env.local.yaml`.

#### What it costs

Measured on a settled instance with mit-learn, mitxonline and ocw-studio also
running:

| | |
|---|---|
| Memory at rest | **~2.6 GiB** |
| CPU at rest | under 0.1 core |
| CPU while booting | ~1.6 cores |
| First start | ~2 minutes, from empty volumes |
| Later starts | ~80 seconds, reusing the volumes |
| Disk | 20 GiB data + 1 GiB config PVC, plus a 1.5 GB image |

Where the memory goes: two Ruby processes at roughly 1 GiB each (puma and
sidekiq), Gitaly at ~345 MiB, the bundled PostgreSQL at ~140 MiB across its
processes, and Workhorse at ~60 MiB.

That is the single largest workload in local-dev. For comparison, the whole
rest of the cluster with three apps running sits around 21 GiB across the
three nodes, so GitLab is roughly an eighth of the total on its own. If you
are near your memory ceiling, this is the first thing to turn off.

#### Why it is configured the way it is

Omnibus ships a full observability stack, a container registry, a Kubernetes
agent server and Pages, none of which this setup uses, and each is a separate
process holding memory. `configmaps/omnibus.yaml` turns them off and runs puma
in single-process mode with a small sidekiq pool, which is what makes the
footprint above achievable rather than the 4 GB GitLab asks for as a minimum.

`nginx['listen_port'] = 80` and `nginx['listen_https'] = false` are both
load-bearing. APISIX terminates TLS and forwards plain HTTP, but `external_url`
has to be the public https URL or GitLab builds `http://` links and mangles
API pagination. Set the https URL without those two and Omnibus serves 443
itself and redirects http to https, which against a TLS-terminating proxy is an
infinite redirect loop.

GitLab keeps its own bundled PostgreSQL and Redis rather than using the shared
cluster, unlike every other app here. Omnibus creates extensions at install
time, which needs a superuser, and the shared `app` role is not one. The cost
is that GitLab's data lives in its own PVC instead of alongside everything
else in Postgres.

#### Removing it

Taking `gitlab` out of `enabled_apps` tears down the deployment on the next
reconcile but deliberately leaves the volumes, so re-enabling it keeps your
repositories. That is why the `gitlab` namespace is provisioned unconditionally
while the workload is gated: deleting a namespace takes its
PersistentVolumeClaims with it, and no amount of StatefulSet volume retention
survives that.

To reclaim the disk as well:

```bash
kubectl -n gitlab delete pvc data-gitlab-0 config-gitlab-0
```

That loses GitLab's git history and means another first boot, but not the
content itself: Studio's copy is in the shared Postgres, and it can rebuild
the repositories with `reset_sync_states --create_backends` followed by
`sync_website_to_backend --force-create`.

### OCW theme development (ocw-hugo-themes)

Run the Hugo dev server the way you always have, on your own machine:

```
cd ocw-hugo-themes && yarn start
```

For ordinary frontend work, `http://localhost:3000` is fine and nothing here
applies. Use `https://ocw-dev.learn.<root_domain>` instead when you need a
logged-in session: bookmarks, the user menu, anything that calls the mit-learn
API as a real user. That hostname is APISIX proxying to the dev server still
running on port 3000 on your machine; no part of Hugo moves into the cluster,
and the route simply 502s while the dev server is stopped.

The reason localhost cannot do it is cookie scope, not CORS. mit-learn issues
`csrftoken` with `Domain=.learn.<root_domain>`, and the theme reads that cookie
out of `document.cookie` through axios (`xsrfCookieName`). A page on localhost
cannot read another domain's cookie, so it never sends `X-CSRFToken` and every
write fails; the APISIX session cookie is `SameSite=Lax` on
`.learn.<root_domain>`, so it is not sent from a cross-site origin either and
the user reads as anonymous. An origin inside `.learn.<root_domain>` has neither
problem.

Re-run `./local-dev/scripts/setup.sh` if your `/etc/hosts` predates this: the
hostname needs an entry. The TLS certificate already covers it through the
`*.learn.<root_domain>` wildcard.

### learn-ai sandbox (frontend-demo)

With a learn-ai checkout, Tilt builds `learn-ai/frontend-demo` as a static
export and serves it with nginx at `https://ai-sandbox.learn.<root_domain>`,
the way CI, QA and Production serve it. There is no published image, so
without a checkout the sandbox is not deployed. The `learnai-webapp` row in the
Tilt UI links to it and to the learn-ai Django admin.

`learnai-sandbox` is a manual resource. Saving a file never starts a `next
build` (about a minute, several GiB of memory); click the resource's update
button in the Tilt UI when you want your changes, and after editing
`configmaps/sandbox-env.local.yaml`. It still builds once on `tilt up`, after
`learnai-webapp`.

The sandbox calls learn-ai at `https://api.learn.<root_domain>/ai`, so it shares
mit-learn's login, and mit-learn at `/learn/`. Search goes to production, as
every deployed sandbox does. The `NEXT_PUBLIC_*` values are build args in
`apps/learn-ai/Tiltfile`. It is served on a `.learn` host, not localhost, for
the cookie reasons under
[OCW theme development](#ocw-theme-development-ocw-hugo-themes).

Open edX content (video and Canvas panels) goes through nginx's
`/openedx_proxy/` to `courses.rc.learn.mit.edu`. To be logged in there, copy
`apps/learn-ai/configmaps/sandbox-env.local.yaml.example` to
`sandbox-env.local.yaml` and set your RC LMS session cookie. The proxy passes
only the two `GET`s the sandbox makes.

Re-run `./local-dev/scripts/setup.sh` if your `/etc/hosts` predates this; the
TLS certificate already covers the hostname.

---

## Troubleshooting

### `tilt up` fails with `specified unknown setting name 'openedx_mode'`

`config.parse()` rejects any key in `tilt_config.json` that the Tiltfile does not declare, so the whole Tiltfile fails to load. `openedx_mode` was never wired to anything and has been removed — delete the key from your `tilt_config.json`. See [Open edX](#open-edx) for where that stack lives now.

### OCW Studio pipelines never start a build

The Concourse worker runs its own containerd to execute pipeline tasks, which means a
container runtime nested inside the k3s node container. It is the only privileged
workload in local-dev and the first thing to suspect when builds sit pending.

Check the worker registered at all:

```bash
kubectl --context local-dev -n ocw-studio logs deploy/ocwstudio-concourse -c worker | grep beacon.registered
```

Then confirm the web half agrees, from the Concourse UI's workers page or:

```bash
kubectl --context local-dev -n ocw-studio logs deploy/ocwstudio-concourse -c web | grep forward-worker | tail -1
```

If the worker registers but tasks fail to start, the baggageclaim driver is the usual
cause. It is set to `naive` in `concourse.yaml` precisely because the worker's
filesystem is already an overlay and stacking another overlay mount on it is not
supported everywhere — `naive` copies instead, which is slower but portable. Switching
it back to `overlay` is a reasonable thing to try on a machine where it works.

Restarting the `ocwstudio-concourse` resource is safe: the TSA keypair is regenerated
on every pod start and is only ever used inside that pod.

### `tilt up` fails on `local-infra` (Pulumi errors)

```bash
# Verbose core stack run (LOCAL_DEV_ENABLED_APPS must list your enabled_apps;
# an empty value is fine if you run no optional apps)
cd local-dev/infra/core
PULUMI_CONFIG_PASSPHRASE='' LOCAL_DEV_ENABLED_APPS="${LOCAL_DEV_ENABLED_APPS:-}" \
  pulumi up --stack local-dev.core.Dev --logtostderr -v=3

# Verbose apps_infra stack run
cd local-dev/infra/apps_infra
PULUMI_CONFIG_PASSPHRASE='' pulumi up --refresh --stack local-dev.apps-infra.Dev --logtostderr -v=3
```

Common causes:
- **kubeconfig not set:** The stacks target the `local-dev` context, so `kubectl config get-contexts local-dev` must succeed. If it does not, run `k3d kubeconfig merge local-dev --kubeconfig-merge-default`, then `kubectl config rename-context k3d-local-dev local-dev` (k3d names the context `k3d-local-dev`; `setup.sh` renames it). If your `~/.kube` directory is a symlink pointing to a Windows-side path (common in WSL2), see [WSL2: kubeconfig context not found](#wsl2-kubeconfig-context-not-found) below.
- **Cert files missing:** Run `setup.sh --skip-hosts` to regenerate certs without touching `/etc/hosts`.

### `local-infra-apps` fails with `Missing required argument "client_id"`

```
keycloak:openid/clientServiceAccountRole:ClientServiceAccountRole resource 'olapps-mitxonline-b2b-view-realm'
has a problem: Missing required argument. The argument "client_id" is required, but no definition was found.
```

The b2b client the error names is not the problem. Keycloak is empty — `curl -sk https://sso.ol.mit.test/admin/realms` with an admin token returns only `master` — while the apps-infra Pulumi state still describes the `olapps` realm, so Pulumi skips creating it and dies on the first resource that has to look something up inside it.

That happens when the cluster is replaced without `teardown.sh`: deleted by hand, lost to a `docker system prune` (see [Disk Management](#disk-management)), or to a WSL reset. The state lives in this checkout and survives all of them.

Both infra stacks now `pulumi refresh` before applying, so this repairs itself — refresh drops the resources that no longer exist and `up` rebuilds them, about 20 seconds. `start.sh` also compares the cluster's identity against the one the state was written against and says so on the way past, which is the earlier and more useful signal. If you hit the error anyway, run the refresh by hand:

```bash
cd local-dev/infra/apps_infra
PULUMI_CONFIG_PASSPHRASE='' pulumi refresh --yes --parallel 1 --stack local-dev.apps-infra.Dev
```

The app databases in the new cluster are empty regardless: restore from a dump with [`pg-restore.sh`](#backing-up-and-restoring-postgres) if you have one. The previous cluster's volumes may still be on disk as dangling volumes, but there is no supported way to reattach them to a new k3d cluster — treat them as lost and reclaim the space with `docker volume prune`.

### `local-infra-apps` fails with `409 Conflict` on a Keycloak object

```
error sending POST request to /admin/realms/olapps/clients: 409 Conflict.
Response body: {"errorMessage":"Client ol-mitlearn-client already exists"}
```

Keycloak has an object the apps-infra state does not. A create that Keycloak applied but Pulumi never recorded leaves one behind: the request timed out, or the run was killed (Ctrl-C, `tilt down`) while it was in flight.

The `olapps` realm recovers on its own. Before each run the program asks Keycloak whether the realm exists and, if it does, imports it instead of creating it; Pulumi skips the import once the realm is in state. `local-infra-apps` also runs `pulumi up` a second time when the first attempt fails (`local-dev/scripts/pulumi-up-retry.sh`), so a realm create that times out on a fresh cluster shows one failed attempt in the log followed by one that imports the realm, with nothing to retrigger. A run killed mid-apply (rather than stopped with Ctrl-C) leaves Pulumi's state lock behind; the wrapper removes a lock whose process is gone before each attempt, the way the mitxonline program's apply does. If the realm lookup itself fails (Keycloak unreachable, wrong admin credentials), the run stops with a Python traceback ending in an `httpx` error before Pulumi registers anything, not with a 409.

Nothing inside the realm (a client, client scope, group, organization, identity provider, flow) is adopted. Pulumi stops at the first failed create and `--parallel 1` keeps one in flight, so each run reports one 409 whichever way the state went wrong. Work out which case you are in before deleting anything:

```bash
cd local-dev/infra/apps_infra
PULUMI_CONFIG_PASSPHRASE='' pulumi stack export --stack local-dev.apps-infra.Dev | jq -r '
  (.deployment.pending_operations[]? | "pending: \(.type) \(.resource.urn)"),
  "keycloak resources in state: \([.deployment.resources[] | select(.type | startswith("keycloak:"))] | length)"'
```

_A killed run left one object behind._ The output lists a `pending: creating` entry for the object the 409 names. A create that timed out leaves no entry, but the earlier run's log shows `context deadline exceeded` on that object; treat it the same way. Delete each listed object that exists in the [admin console](https://sso.ol.mit.test/admin/master/console/#/olapps) (log in as `admin` / `admin`), drop the entries, and retrigger `local-infra-apps`; Pulumi recreates the objects from the program. This touches only those objects, not users.

```bash
PULUMI_CONFIG_PASSPHRASE='' pulumi refresh --yes --clear-pending-creates --stack local-dev.apps-infra.Dev
```

The entries are only bookkeeping, so the refresh is also the fix when the realm itself was the interrupted create: the import has already adopted it, and every later run prints `Attempting to deploy or update resources with N pending operations` until they are dropped.

_The state lost track of the realm's contents._ Nothing is pending, no timeout preceded the 409, and the stack holds only a handful of Keycloak resources (the adopted realm and the few that do not conflict) where a stack that has applied once holds about 70 with no optional apps enabled. Something removed the state while the cluster kept its realm, e.g. `local-dev/infra/.pulumi` was deleted, or Tilt ran from a second checkout against the same cluster. Deleting objects one at a time would take a run per object. Either run Tilt from the checkout that created the cluster, or, if nothing in the realm matters to you, delete the realm (this removes every user in it, including ones `pg-restore.sh` imported) and retrigger `local-infra-apps`; its refresh drops the realm from state and the apply rebuilds it:

```bash
token=$(curl -sk --data 'client_id=admin-cli&grant_type=password&username=admin&password=admin' \
  https://sso.ol.mit.test/realms/master/protocol/openid-connect/token | jq -r .access_token)
curl -sk -X DELETE -H "Authorization: Bearer $token" https://sso.ol.mit.test/admin/realms/olapps
```

### App pod stuck in `Init:CrashLoopBackOff`

The initContainer runs migrations; check its logs:

```bash
kubectl --context local-dev logs -n <namespace> <pod-name> -c bootstrap
```

Common causes:
- Database not ready yet (CNPG takes ~30s on first run — Tilt will retry automatically).
- Missing required env var — check the configmap and secrets against the app's `settings.py`.

### APISIX returns 404 for a hostname

```bash
# Check that ApisixRoute was picked up
kubectl --context local-dev get apisixroute -n <namespace>

# Check APISIX ingress controller logs
kubectl --context local-dev logs -n operations deploy/apisix-ingress-controller -f
```

The ingress controller watches for `ApisixRoute` CRDs and syncs them to the APISIX data plane. A restart of the ingress controller pod often resolves sync issues.

### Keycloak login loop / OIDC errors

Keycloak takes 60–90 seconds to start on first boot (database schema migration). Check its readiness:

```bash
kubectl --context local-dev get pod -n local-infra -l app=keycloak
kubectl --context local-dev logs -n local-infra -l app=keycloak -f
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

### App pods stuck in `ImagePullBackOff` with `number of layers and diffIDs don't match`

```bash
kubectl --context local-dev -n mit-learn describe pods | grep diffIDs
# Failed to pull image "k3d-registry.localhost:5000/mitodl_mit-learn-app:tilt-...":
#   ... number of layers and diffIDs don't match: 17 != 22
```

The cluster's nodes run a k3s image with containerd 1.7, which does not understand the zstd layer media type that Tilt's `docker_build` emits for images based on `mitodl/ol-python-base` (see the comment on `image:` in `local-dev/cluster/k3d-config.yaml`). Nothing about the registry or the build is wrong; the node image is too old. k3d cannot change a running cluster's node image, so recreate it: follow [Backing Up and Restoring Postgres](#backing-up-and-restoring-postgres), which covers the teardown, `setup.sh` picking up the new image, and carrying your Postgres data across.

Already-pushed images pull fine on the new nodes without a rebuild — the registry volume is separate from the cluster and survives teardown.

### Docker image build fails (Next.js)

The Next.js build needs ~4 GB of memory. If it OOMs:
- Increase the Docker VM memory allocation (see [Prerequisites](#prerequisites))
- Or skip the build: with no `mit-learn` checkout next to `ol-infrastructure`, Tilt runs the published `mitodl/mit-learn-nextjs-app` image (and the published backend image) instead of building

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

### Image pulls and builds fail with DNS i/o timeout after changing networks

Pods sit in `ImagePullBackOff` and dagger-backed Tilt builds (`codejail`, `mfe-*`) fail at `Container.from`, all with a DNS timeout on the registry:

```
failed to resolve reference "docker.io/library/python:3.12-slim-trixie":
dial tcp: lookup registry-1.docker.io on 10.87.0.1:53:
read udp 10.87.0.1:47380->10.87.0.1:53: i/o timeout
```

**Why:** Docker writes a container's `/etc/resolv.conf` when the container *starts*, from the host's resolver config at that moment, and never revisits it. Move the host between networks — home wifi to the office, VPN up or down — and every long-running container keeps forwarding DNS to resolvers that are no longer reachable. Nothing in the repo is broken, which is what makes this one confusing: the containers stay up, the nodes report `Ready`, and only name resolution is dead.

A detail worth knowing, because it misleads: the resolver list usually ends in public resolvers (`8.8.8.8`, `8.8.4.4`) that *do* answer from any network, so the failure is not that every resolver is dead — it's that the dead ones come first and burn the client's whole timeout budget before a working one is reached. dagger's own logs show it abandoning the resolve at ~20s; containerd surfaces the same thing as a bare `Try again` (`EAI_AGAIN`).

Those public entries come from your *host's* config, not from Docker: for k3d nodes, moby copies the host file's nameservers into the node's `ExtServers` verbatim and adds nothing of its own. (It does substitute its own defaults, but only on the legacy bridge and only when no nameserver survives filtering — a path these nodes never take.) So a `host(8.8.8.8)` in a node's `resolv.conf` is a host resolver, and `cat /etc/resolv.conf` on the host will show it.

To confirm, compare a container against the host — a stale one shows the old network's search domain and upstreams:

```bash
docker exec k3d-local-dev-server-0 cat /etc/resolv.conf   # e.g. search hsd1.ma.comcast.net
cat /etc/resolv.conf                                      # e.g. search mit.edu
```

**Fix:** run the heal script. It compares each container's baked resolvers against the host's current ones and `docker restart`s only the containers that actually drifted — a restart re-bakes `resolv.conf`, which is the whole fix. Like `heal-exec.sh` it avoids `k3d cluster stop/start`, so node IPs are preserved. It's a no-op when nothing has drifted:

```bash
./local-dev/scripts/heal-dns.sh
```

`start.sh` runs this automatically, before the exec heal.

Two things the script has to work around, worth knowing if you are reading its output:

- **systemd-resolved hosts** (the Ubuntu and Fedora default) publish a stub `/etc/resolv.conf` listing only `127.0.0.53`. The real upstreams live in `/run/systemd/resolve/resolv.conf`, and that is the file Docker reads when it sees the stub — so the script follows the same rule. Comparing against the stub instead would make it skip every container on exactly the hosts that need checking.
- **It only runs where the Docker daemon is on this machine.** Detection compares each container's resolvers against the host's and probes them from the host, which only means anything when the daemon shares our network namespace. When Docker runs in a VM — macOS, Docker Desktop, WSL2 — nodes carry the VM's own forwarder instead, which is in neither the host's list nor reachable from it. Restarting cannot change that (the forwarder address is stable), and does not need to: the forwarder follows the host's DNS, so lookups keep working across a network switch. The script compares `docker info --format '{{.Name}}'`, the daemon's own hostname, against `uname -n`, and skips when they differ. Hostnames rather than kernel versions on purpose: every WSL2 distro shares one kernel, Docker Desktop's included, so a kernel comparison reads WSL2 as native while its containers still resolve through the VM.

Pods already in `ImagePullBackOff` retry on kubelet's backoff timer (up to ~5m) rather than immediately, so a still-red Tilt right after a heal doesn't mean the heal failed. To retry at once, delete the pod:

```bash
kubectl --context local-dev delete pod -n <namespace> <pod>
```

**Automatic (Linux):** install the two example user units — no root, nothing outside `$HOME`:

```bash
mkdir -p ~/.config/systemd/user
for u in local-dev-heal.service local-dev-heal.timer; do
  sed "s|<checkout>|$PWD|g" "local-dev/scripts/systemd/$u.example" \
    > ~/.config/systemd/user/"$u"
done
systemctl --user daemon-reload
systemctl --user enable --now local-dev-heal.timer
```

`journalctl --user -u local-dev-heal` for output.

This polls every five minutes rather than hooking an event, which is deliberate. The trigger for stale container DNS is a *network* change, and bringing a VPN up or switching wifi involves no suspend at all — so a wake hook would miss exactly the cases above. `Persistent=true` makes it catch up a run missed while the machine was suspended, which covers the close-the-lid case too. The heal is a no-op when nothing has drifted, so a five-minute tick is cheap. A switch mid-interval is healed on the next tick, or immediately by running the script by hand.

Two notes on why it is a *user* timer and not a sleep hook: `sleep.target` exists only in the system manager, so a user unit can never be pulled in by it (it would fail silently); and logind's `PrepareForSleep` D-Bus signal, the proper user-space wake hook, needs a client library that is not reliably installed. Distros that manage units declaratively express the same two units through their own configuration.

On macOS this heal self-skips (see above), so there is nothing to schedule.

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
ln -sf "$PWD/local-dev/scripts/wakeup-macos.example.sh" ~/.wakeup
brew services start sleepwatcher
```

sleepwatcher runs `~/.wakeup` on every wake; the example hook calls `heal-exec.sh`, logging to `~/Library/Logs/local-dev-heal.log`. No editing needed — it derives the checkout path from its own location, resolved through the symlink. It deliberately does not call `heal-dns.sh`, which self-skips whenever Docker runs in a VM.

If you symlinked `~/.wakeup` before this script was renamed from `wakeup.example.sh`, re-point it: the old path now dangles and sleepwatcher fails silently.

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
kubectl config delete-context local-dev 2>/dev/null || true  # a stale entry from an earlier cluster
kubectl config rename-context k3d-local-dev local-dev
kubectl config get-contexts local-dev   # Should now succeed
```

### WSL2: every image build fails with `error getting credentials`

```
Build Failed: docker push: ... authInfo#RetrieveAuthTokenFromImage: error getting credentials - err: exit status 1, out: ``
```

Docker Desktop sets `"credsStore": "desktop.exe"` in `~/.docker/config.json`, and Tilt asks that Windows binary for credentials before every push and pull. When WSL's interop channel to Windows is unhealthy the helper fails with no output, and unlike `docker push`, Tilt treats that as fatal. Confirm it with:

```bash
echo localhost:5001 | docker-credential-desktop.exe get
# <3>WSL (...) ERROR: UtilAcceptVsock:271: accept4 failed 110
```

Either reset the interop channel from Windows PowerShell with `wsl --shutdown` and start again, or remove the `credsStore` key from `~/.docker/config.json` so Tilt never calls the helper (Docker Desktop re-adds it on restart). The local registry needs no credentials, so anonymous pushes work. See [mitodl/hq#13310](https://github.com/mitodl/hq/issues/13310).

### `/etc/hosts` entries disappear after WSL restart

WSL2 regenerates `/etc/hosts` by default. `setup.sh` sets `generateHosts = false` in `/etc/wsl.conf` automatically, but the change only takes effect after restarting WSL. From Windows PowerShell run:

```powershell
wsl --shutdown
```

Then reopen your WSL terminal. If `setup.sh` has already run, the entries will persist from that point on.

### Windows browser can't resolve `mit.test` hostnames

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
