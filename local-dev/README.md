# MIT Learn Local Development Environment

A fully local, Kubernetes-based development environment for the MIT Learn application stack, running in [k3d](https://k3d.io) with [Tilt](https://tilt.dev) for live development and Pulumi for shared infrastructure.

---

## Table of Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Quick Start](#quick-start)
4. [Architecture](#architecture)
5. [Directory Structure](#directory-structure)
6. [How It Works](#how-it-works)
7. [Working with Apps](#working-with-apps)
8. [Seeding Data](#seeding-data)
9. [Configuration Reference](#configuration-reference)
10. [Adding a New App](#adding-a-new-app)
11. [Modifying Shared Infrastructure](#modifying-shared-infrastructure)
12. [Teardown](#teardown)
13. [Troubleshooting](#troubleshooting)

---

## Overview

This environment runs four applications as Kubernetes workloads inside a local k3d cluster:

| App | Local URL | Description |
|-----|-----------|-------------|
| mit-learn (frontend) | `https://learn.mit.dev` | Next.js frontend |
| mit-learn (backend) | `https://api.learn.mit.dev` | Django/granian API |
| learn-ai | `https://ai.learn.mit.dev` | Django AI proxy service |
| mitxonline | `https://mitxonline.mit.dev` | MITx Online LMS (Django/uwsgi) |
| odl-video-service | `https://video.odl.mit.dev` | ODL Video Service (Django/uwsgi) |
| Keycloak SSO | `https://sso.ol.mit.dev` | Identity provider (olapps realm) |

All hostnames use a `.dev` TLD that mirrors production (`.edu` → `.dev`), so URLs, CSRF cookies, and OIDC redirect URIs behave identically to deployed environments.

**Design goals:**
- `setup.sh` does the minimum necessary outside the cluster (k3d, certs, /etc/hosts). Everything else is in-cluster.
- Pulumi owns all in-cluster shared resources. Tilt owns app deployments.
- Live source sync for Django apps (no container rebuild needed for Python changes).
- Pre-built image fallback when a source repo is not checked out.

---

## Prerequisites

Install these tools before running setup:

| Tool | Version | Install |
|------|---------|---------|
| Docker Desktop | ≥ 4.x (8 GB RAM allocated) | https://docs.docker.com/desktop/ |
| kubectl | ≥ 1.28 | `brew install kubectl` |
| k3d | ≥ 5.7 | `brew install k3d` |
| Tilt | ≥ 0.33 | https://docs.tilt.dev/install.html |
| Helm | ≥ 3.14 | `brew install helm` |
| mkcert | ≥ 1.4 | `brew install mkcert` |
| Pulumi CLI | ≥ 3.x | `brew install pulumi` |
| uv | ≥ 0.9.3 | `brew install uv` |

> **Docker memory:** The cluster runs PostgreSQL, Valkey, APISIX, Keycloak, Qdrant, and up to four Django apps. Allocate at least 8 GB to Docker Desktop (Settings → Resources).

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

For per-developer app env vars and secrets (API keys, feature flags), don't
edit the tracked manifests — drop a gitignored `app-env.local.yaml` ConfigMap
next to the app's tracked one instead. See
[Local Configuration Overrides](#local-configuration-overrides).

### 3. Start the environment

The easiest way is to use the provided start script, which validates your setup and syncs dependencies:

```bash
./local-dev/scripts/start.sh
```

This will:
1. Validate that `setup.sh` has been run (cluster exists, kubeconfig configured, certs present)
2. Heal any wedged kubelet exec/streaming (see [Troubleshooting](#kubectl-exec-fails-with-a-502-wedged-kubelet-streaming)) — a no-op when healthy
3. Sync Python dependencies via `uv`
4. Start Tilt

**Alternative:** If you prefer to start manually without the validation wrapper:

```bash
uv sync
tilt up
```

### 4. Monitor the environment

Tilt will:
1. Run `pulumi up` to deploy shared infrastructure (Postgres, Valkey, APISIX, Keycloak, etc.)
2. Build Docker images for any checked-out app repos
3. Apply all app manifests (Deployments, Services, ConfigMaps, APISIX routes)
4. Watch for source changes and sync them live

Open the Tilt UI at `http://localhost:10350` to monitor deployments and trigger seeds.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  k3d cluster: local-dev                                       │
│                                                               │
│  ┌──────────┐  ┌──────────────────────────────────────────┐  │
│  │operations│  │           local-infra                    │  │
│  │          │  │  PostgreSQL (CNPG)  Valkey  Qdrant         │  │
│  │  APISIX  │  │  Keycloak  LiteLLM  Mailpit              │  │
│  └────┬─────┘  └──────────────────────────────────────────┘  │
│       │                                                       │
│       │  routes traffic by hostname                          │
│       ├──────────────────────────────────────────────────┐   │
│       │                                                  │   │
│  ┌────▼──────┐  ┌───────────┐  ┌──────────┐  ┌────────┐ │   │
│  │ mit-learn │  │ learn-ai  │  │mitxonline│  │  odl-  │ │   │
│  │  (ns)     │  │   (ns)    │  │   (ns)   │  │ video  │ │   │
│  │ Next.js   │  │ granian   │  │  uwsgi   │  │  uwsgi │ │   │
│  │ Django    │  │ Celery×2  │  │  Celery  │  │ Celery │ │   │
│  │ Celery×3  │  │           │  │          │  │        │ │   │
│  └───────────┘  └───────────┘  └──────────┘  └────────┘ │   │
└──────────────────────────────────────────────────────────────┘
         ▲
         │  HTTPS (mkcert TLS, trusted by OS)
    Developer browser / curl
```

### Key design decisions

**Ownership boundary:** `setup.sh` owns only the k3d cluster, TLS certificates, and `/etc/hosts`. _All_ in-cluster resources are owned by either Pulumi (shared infra) or Tilt (app manifests). This prevents drift and conflicts.

**APISIX as ingress:** Traefik is disabled in k3d. APISIX handles all ingress, TLS termination, and OIDC authentication (via the `openid-connect` plugin). Each app's `apisix-routes.yaml` declares its `ApisixRoute` and `ApisixTls` CRDs.

**Shared database cluster:** All apps share one CloudNativePG (CNPG) cluster in the `local-infra` namespace with isolated databases (`mitlearn`, `learnai`, `mitxonline`, `odlvideo`, `keycloak`, `litellm`). This keeps memory usage low. The `per_app_databases` toggle (Phase 6A, not yet implemented) will allow fully isolated CNPG clusters per namespace.

**TLS:** mkcert generates a wildcard certificate for all `.dev` domains. The cert is read by Pulumi at stack evaluation time and stored as `local-dev-tls` Kubernetes Secrets in every app namespace. APISIX's `ApisixTls` CRs reference these secrets.

**Keycloak realm:** The `olapps` realm mirrors production, including the fake-Touchstone SAML IdP, all OIDC clients, and organizations support. Test users: `admin@odl.local`, `student@odl.local`, `prof@odl.local` (password: `localdev123`).  <!-- pragma: allowlist secret -->

---

## Directory Structure

```
ol-infrastructure/
├── Tiltfile                          # Root entry point; wires all app Tiltfiles
├── tilt_config.json.example          # Developer config template (copy → tilt_config.json)
│
└── local-dev/
    ├── cluster/
    │   └── k3d-config.yaml           # k3d cluster definition (ports, registry, no Traefik)
    │
    ├── scripts/
    │   ├── setup.sh                  # One-time bootstrap (cluster, certs, /etc/hosts)
    │   ├── start.sh                  # Start the environment (validate setup, heal exec, sync deps, tilt up)
    │   ├── stop.sh                   # Pause the cluster (fast resume via start.sh)
    │   ├── teardown.sh               # Destroy the cluster
    │   ├── seed.sh                   # CLI seeding wrapper (kubectl exec into pods)
    │   ├── heal-exec.sh              # Repair wedged kubelet exec/streaming after sleep
    │   └── wakeup.example.sh         # Example sleepwatcher wake hook (macOS auto-heal)
    │
    ├── certs/                        # mkcert output (gitignored)
    │   ├── local-dev.pem
    │   ├── local-dev-key.pem
    │   └── rootCA.pem
    │
    ├── infra/                        # Pulumi stacks — shared in-cluster infrastructure
    │   ├── core/                     # Core stack: operators, Keycloak, APISIX, DB, Valkey
    │   │   ├── Pulumi.yaml
    │   │   ├── Pulumi.local-dev.core.Dev.yaml   # Stack config (chart versions, Keycloak hostname)
    │   │   └── __main__.py
    │   └── apps_infra/               # Apps-infra stack: Keycloak realm + OIDC clients
    │       ├── Pulumi.yaml
    │       ├── Pulumi.local-dev.apps-infra.Dev.yaml   # Stack config (client secrets)
    │       └── __main__.py
    │
    └── apps/
        ├── mit-learn/                # Django backend manifests
        │   ├── Tiltfile
        │   ├── deployment.yaml       # Web + 3 Celery workers + Beat
        │   ├── secrets.yaml
        │   ├── apisix-routes.yaml    # Routes for api.learn.mit.dev
        │   └── configmaps/
        │       ├── app-env.yaml      # Non-secret env vars
        │       ├── app-env.local.yaml         # (optional, gitignored) your overrides
        │       ├── app-env.local.yaml.example
        │       └── nginx.yaml        # nginx sidecar config
        │
        ├── mit-learn-nextjs/         # Next.js frontend manifests
        │   ├── Tiltfile              # docker_build with NEXT_PUBLIC_* build args
        │   ├── deployment.yaml
        │   └── apisix-routes.yaml    # Route for learn.mit.dev
        │
        ├── learn-ai/                 # Django AI proxy manifests
        │   ├── Tiltfile
        │   ├── deployment.yaml       # Web + 2 Celery workers + Beat
        │   ├── secrets.yaml
        │   ├── apisix-routes.yaml    # Route for ai.learn.mit.dev
        │   └── configmaps/
        │
        ├── mitxonline/               # MITx Online LMS manifests
        │   ├── Tiltfile
        │   ├── deployment.yaml       # Web + Celery + Beat
        │   ├── secrets.yaml
        │   ├── apisix-routes.yaml    # Route for mitxonline.mit.dev
        │   └── configmaps/
        │
        └── odl-video-service/        # ODL Video Service manifests
            ├── Tiltfile
            ├── deployment.yaml       # Web + Celery + Beat
            ├── secrets.yaml
            ├── apisix-routes.yaml    # Route for video.odl.mit.dev
            └── configmaps/
```

---

## How It Works

### Startup sequence

```
tilt up
  │
  ├── pulumi up (local-dev/infra/)
  │     Deploys: CNPG operator → PostgreSQL cluster → Valkey → cert-manager
  │              → Qdrant → LiteLLM → Mailpit → APISIX
  │              → Keycloak operator → Keycloak instance → olapps realm
  │
  └── for each enabled app:
        ├── docker build (if source repo present, else pull prebuilt image)
        ├── kubectl apply configmaps/ secrets.yaml deployment.yaml
        │     initContainer: migrate + collectstatic + data-specific seeds
        └── kubectl apply apisix-routes.yaml
              APISIX picks up new routes → app reachable at its .dev URL
```

### Live updates (Django apps)

When a file changes in a checked-out app repo, Tilt syncs it directly into the running container without a rebuild (sub-second). granian runs with `--reload`, notices the change, and restarts its workers — the new code serves once Django finishes re-importing (roughly 10–30 s depending on the app). Watch for `Changes detected, reloading workers..` in the app logs.

This applies to the webapp (granian) containers. Celery workers and beat don't auto-reload — restart those resources from the Tilt UI after changing task code.

When `pyproject.toml` or `uv.lock` changes, Tilt runs `uv sync` inside the container; granian then reloads as above.

### Pre-built image fallback

If an app repo is not cloned on your machine, Tilt uses the pre-built ECR image at the tag listed in `tilt_config.json` under `prebuilt_tags`. You can work on mit-learn without having learn-ai checked out.

### Live updates (Next.js frontend)

With a mit-learn checkout, Tilt builds the `local-dev` stage of `Dockerfile.web`, which runs `next dev` (Turbopack). Source changes under `frontends/` are live-synced into the container and hot-reload in place — edit-to-served is roughly a second (the first request to each page after a pod start pays a one-time on-demand compile). HMR websockets are proxied through apisix, so the browser hot-updates on `https://learn.mit.dev` too. `NEXT_PUBLIC_*` values are runtime env vars (see `deployment.yaml`), not build args, so no rebuild is needed to change them — just edit and let Tilt redeploy.

When `yarn.lock` changes, Tilt runs `yarn install` inside the container.

Without a checkout, the prebuilt DockerHub image (production `runner` stage) serves instead; it does not hot-reload.

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

### Access logs

```bash
# Web pod
kubectl logs -n mit-learn deploy/mitlearn-webapp -c app -f

# Celery worker
kubectl logs -n mit-learn deploy/mitlearn-worker-default -f

# APISIX ingress
kubectl logs -n operations deploy/apisix -f
```

### Run management commands

```bash
kubectl exec -n mit-learn deploy/mitlearn-webapp -- python manage.py shell
kubectl exec -n learn-ai deploy/learnai-webapp -- python manage.py dbshell
```

### Connect to PostgreSQL directly

```bash
kubectl exec -n local-infra local-pg-1 -- psql -U app -d mitlearn
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
| learn-ai | `seed-learn-ai-checkpoints` | Backpopulate tutor checkpoints |
| mitxonline | `seed-mitxonline-instance` | Full instance setup (superuser, courses, products) |
| mitxonline | `seed-mitxonline-course-data` | Populate test course data |
| mitxonline | `seed-mitxonline-income-thresholds` | Load country income thresholds |
| odl-video-service | `seed-ovs-presets` | Create encoding presets (requires real AWS creds) |

---

## Configuration Reference

`tilt_config.json` (copy from `tilt_config.json.example`):

```json
{
  "enabled_apps": ["mit-learn", "learn-ai", "mitxonline", "odl-video-service"],

  "per_app_databases": false,

  "prebuilt_tags": [
    "mit-learn=0.62.0",
    "mit-learn-nextjs=0.62.0",
    "learn-ai=0.28.3",
    "mitxonline=1.144.5",
    "odl-video-service=0.85.0"
  ]
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `enabled_apps` | all four | Apps to deploy. Omit any to skip it entirely. |
| `per_app_databases` | `false` | `true` deploys isolated CNPG + Valkey per namespace (Phase 6A). |
| `prebuilt_tags` | see file | `["app=tag"]` list of image tags used when the app repo is not checked out locally. |

The rule of thumb for which config surface a knob belongs to: settings that
change **which/how Tilt runs things** (apps, image tags, domain) go in
`tilt_config.json`; anything that sets an **env var or secret value inside a
workload** (API keys, feature flags, endpoints) goes in a gitignored
`app-env.local.yaml` override ConfigMap — see [Local Configuration Overrides](#local-configuration-overrides).
(`openai_api_key`, `langsmith_api_key`, and `canvas_ai_token` used to be
listed here but were never wired to anything and have been removed — delete
them from your `tilt_config.json` if present, or `config.parse()` will error.)

### Pulumi stack config

The infrastructure is split across two Pulumi stacks:

**`local-dev/infra/core/Pulumi.local-dev.core.Dev.yaml`** — operators, Keycloak, APISIX, DB, Valkey:

| Key | Default | Description |
|-----|---------|-------------|
| `keycloak_hostname` | `sso.ol.mit.dev` | Keycloak ingress hostname |
| `tls_cert_path` | `local-dev/certs/local-dev.pem` | mkcert cert (relative to repo root) |
| `apisix_version` | `2.12.0` | APISIX Helm chart version |
| `cnpg_version` | `0.23.0` | CloudNativePG operator Helm chart version |
| `keycloak_operator_version` | `26.0.7` | Official Keycloak Operator version |

**`local-dev/infra/apps_infra/Pulumi.local-dev.apps-infra.Dev.yaml`** — Keycloak realm and OIDC clients:

| Key | Default | Description |
|-----|---------|-------------|
| `*_client_secret` | `local-dev-*-secret` | OIDC client secrets (change if needed) |
| `apisix_oidc_session_secret` | `local-dev-oidc-session-secret-32chars!` | Session encryption key (kept for reference) |

---

## Adding a New App

### 1. Create the app manifests directory

```
local-dev/apps/<app-name>/
├── Tiltfile
├── deployment.yaml       # Deployment(s) + Service
├── secrets.yaml          # Placeholder k8s Secrets
├── apisix-routes.yaml    # ApisixTls + ApisixRoute
└── configmaps/
    ├── app-env.yaml      # Non-secret env vars
    ├── app-env.local.yaml.example  # Template for per-dev overrides
    └── nginx.yaml        # (if using nginx sidecar)
```

Use an existing app (e.g., `learn-ai/`) as a template. In particular, copy
the `envFrom` pattern from an existing `deployment.yaml`: every container
lists the tracked ConfigMap, the tracked Secret, then the optional
`<app>-env-local` override ConfigMap **last** (see
[Local Configuration Overrides](#local-configuration-overrides)), and pass
`local_overrides='configmaps/app-env.local.yaml'` to `k8s_yaml_local` in the
Tiltfile.

### 2. Add the app database to the CNPG cluster

In `local-dev/infra/__main__.py`, add to `postInitSQL`:

```python
"CREATE DATABASE myapp OWNER app;",
```

### 3. Copy the TLS secret to the new namespace

In `__main__.py`, add:

```python
tls_secret_myapp = _tls_secret(
    "local-dev-tls-myapp", "my-app", _app_namespaces["my-app"]
)
```

And add `"my-app"` to the namespace loop near the top of `__main__.py`.

### 4. Add the Keycloak OIDC client (if needed)

In `local_dev_keycloak.py`, add a new client and call `_make_oidc_secret()` to create the OIDC credentials Secret in the app namespace.

### 5. Register in the root Tiltfile

In `Tiltfile`, add an entry to the `APPS` list:

```python
{
    "name": "my-app",
    "dir": "my-app",              # sibling repo directory name
    "namespace": "my-app",
    "deploy_name": "myapp-webapp",
    "image_backend": "mitodl/my-app",
    "prebuilt_tag_backend": "1.0.0",
    "tiltfile": "./local-dev/apps/my-app/Tiltfile",
    "seed_commands": [
        {
            "label": "seed-myapp-data",
            "description": "Load initial data",
            "cmd": "python manage.py loaddata initial_data",
        },
    ],
},
```

### 6. Add hosts and DNS

In `setup.sh`, add the hostname to `HOSTS` and ensure it's covered by a `MKCERT_DOMAINS` wildcard. Re-run `setup.sh` to update `/etc/hosts` and regenerate the cert.

---

## Modifying Shared Infrastructure

Shared infrastructure is split into two Pulumi stacks in `local-dev/infra/`. The `core` stack provisions operators, databases, Valkey, APISIX, and the Keycloak instance. The `apps_infra` stack provisions the Keycloak realm and all OIDC client registrations. Changes here affect all apps.

```bash
# Preview and apply core stack changes
cd local-dev/infra/core
pulumi preview --stack local-dev.core.Dev
pulumi up --stack local-dev.core.Dev

# Preview and apply apps_infra stack changes
cd local-dev/infra/apps_infra
pulumi preview --stack local-dev.apps-infra.Dev
pulumi up --stack local-dev.apps-infra.Dev
```

Tilt also runs `pulumi up` automatically when infra files change. You can also trigger it manually from the Tilt UI (`local-infra-core` and `local-infra-apps` resources).

### Common modifications

**Change a Helm chart version:** Edit the version in `infra/core/Pulumi.local-dev.core.Dev.yaml` and run `pulumi up` in `infra/core/`.

**Add a new shared service:** Add it to `infra/core/__main__.py`. Use the existing Qdrant or Valkey blocks as a reference.

**Modify the Keycloak realm or add a new OIDC client:** Edit `infra/modules/keycloak.py`. On `pulumi up`, pulumi-keycloak will diff the realm state and apply only what changed.

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

> **Note:** The teardown script now calls `pulumi destroy` automatically to clean up Pulumi-managed resources before deleting the cluster. This ensures no orphaned resources are left behind.

---

## Customization & Advanced Setup

### Local Configuration Overrides

The ConfigMaps and Secrets are tracked by the repository. For per-developer
customizations (API keys, feature flags, custom endpoints), each app has an
optional, gitignored override ConfigMap:

```bash
cp local-dev/apps/mitxonline/configmaps/app-env.local.yaml.example \
   local-dev/apps/mitxonline/configmaps/app-env.local.yaml
# then add your overrides under data:, e.g.
#   FEATURE_IGNORE_EDX_FAILURES: "True"
```

How it works — plain Kubernetes, visible in each app's `deployment.yaml`:
every container's `envFrom` list references the override ConfigMap
(`mitxonline-env-local` etc.) **last** and with `optional: true`. Kubernetes
resolves duplicate `envFrom` keys by letting the last source win, so your
overrides beat both the tracked ConfigMap *and* the tracked Secret — secret
values are fine in this file, it never leaves your machine. `optional: true`
means no file → no ConfigMap → no-op for everyone else.

Day-to-day behavior:

- Creating or editing the file mid-session re-applies it — no Tilt restart.
  Pods roll automatically so new values actually take effect: because
  Kubernetes does not restart pods on ConfigMap changes, `tiltlib.star`
  stamps a fingerprint of your overrides onto each Deployment's pod template
  (the `ol.mit.edu/local-overrides-hash` annotation), which is also the
  idiomatic production pattern (cf. Helm `checksum/config` annotations).
- Overridden key **names** (never values) are printed in the **Tiltfile
  resource's log** in the Tilt UI, and your full delta is inspectable
  in-cluster at any time:
  `kubectl get cm -n mitxonline mitxonline-env-local -o yaml`
- Gotchas: the ConfigMap's `metadata.name` must match what `deployment.yaml`
  references (`<app>-env-local` — copy the example, don't type it), and all
  `data:` values must be YAML **strings** (quote things like `"True"` and
  `"8080"`). A typo'd key is applied but ignored by the app. If you *delete*
  the file mid-session, prefer emptying `data:` instead — the already-applied
  ConfigMap can linger in-cluster until `tilt down`.

Scope notes:

- **mit-learn-nextjs** can't participate yet: it has no ConfigMap/Secret —
  its env lives directly in `deployment.yaml`. Moving that env block to a
  ConfigMap would enable it.
- **OpenAI key for LiteLLM** (`local-infra` namespace, Pulumi-managed) is
  separate from the app overlays — create the Secret directly (the
  deployment marks it optional, so this is safe to skip entirely):
  ```bash
  kubectl create secret generic litellm-secrets -n local-infra \
    --from-literal=openai_api_key=sk-your-key  # pragma: allowlist secret
  ```
  (That `openai_api_key` Secret *data key* is unrelated to the old
  `tilt_config.json` key of the same name, which was never wired to anything
  and has been removed.)

### GPU Support for Ollama

If you prefer to run Ollama on your host machine to use GPU acceleration:

1. **Stop the in-cluster Ollama** — point `OLLAMA_ENDPOINT` at your host via
   the app's gitignored `app-env.local.yaml` (see
   [Local Configuration Overrides](#local-configuration-overrides)):
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

**Option 1: Use external MinIO instance** — Run MinIO on your host and point
apps at it via their gitignored `app-env.local.yaml` (see
[Local Configuration Overrides](#local-configuration-overrides)):
```yaml
AWS_ENDPOINT_URL: "http://host.docker.internal:9000"
AWS_ACCESS_KEY_ID: "minioadmin"  # pragma: allowlist secret
AWS_SECRET_ACCESS_KEY: "minioadmin"  # pragma: allowlist secret
```
(Docker Desktop; on Linux use `http://172.17.0.1:9000`.)

**Option 2: Deploy MinIO in-cluster** — Modify `local-dev/infra/__main__.py` to add a MinIO Helm chart and patch the ConfigMaps accordingly.

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
- Increase Docker Desktop memory to 10+ GB
- Or use a prebuilt image by removing `mit-learn` from `enabled_apps` in `tilt_config.json` and letting Tilt use the `prebuilt_tags` value instead

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

Edit `local-dev/cluster/k3d-config.yaml` to change `hostPort` to an unused port and update the mirror entry to match, then re-run `setup.sh`.

### Linux: inotify limit exceeded

Tilt watches source files and uses inotify for change detection. On Linux, the default inotify limit may be too low for watching the entire Tilt workspace. If you see errors like `watch ENOSPC` or "No space left on device", increase the limit:

```bash
# Increase inotify watch limit (recommended: 100k for large workspaces)
sudo sysctl fs.inotify.max_user_watches=100000

# Make it permanent (add to /etc/sysctl.conf)
echo 'fs.inotify.max_user_watches=100000' | sudo tee -a /etc/sysctl.conf
```

---

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
