# Extending the Local Development Environment

How to add a new app to the stack or change the shared in-cluster infrastructure. For day-to-day usage see the [README](README.md); for how the pieces fit together see [ARCHITECTURE.md](ARCHITECTURE.md).

## Table of Contents

1. [Adding a New App](#adding-a-new-app)
2. [Composing a Stack from Another Repo](#composing-a-stack-from-another-repo)
3. [Modifying Shared Infrastructure](#modifying-shared-infrastructure)

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

Use an existing app (e.g., `learn-ai/`) as a template. In particular, copy the `envFrom` pattern from an existing `deployment.yaml`: every container lists the tracked ConfigMap, the tracked Secret, then the optional `<app>-env-local` override ConfigMap **last** (see [Local Configuration Overrides](README.md#local-configuration-overrides) in the README), and pass `local_overrides='configmaps/app-env.local.yaml'` to `k8s_yaml_local` in the Tiltfile.

### 2. Add the app database to the CNPG cluster

In `local-dev/infra/modules/database.py`, add to the `postInitSQL` list:

```python
"CREATE DATABASE myapp OWNER app;",
```

### 3. Register the namespace (TLS secret comes with it)

Add `"my-app"` to the `APP_NAMESPACES` tuple in `local-dev/infra/modules/namespaces.py`, and to the matching `app_namespaces` tuple in `local-dev/infra/modules/tls.py` — the latter's loops then create the `local-dev-tls` Secret and mkcert CA ConfigMap in the new namespace automatically.

### 4. Add the Keycloak OIDC client (if needed)

In `local-dev/infra/modules/keycloak.py`, add a new client and call `_make_oidc_secret()` to create the OIDC credentials Secret in the app namespace.

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

## Composing a Stack from Another Repo

The steps above are for an app whose manifests live here. When another repo
already owns a full Tilt stack, compose it instead of reimplementing it — the
`openedx` app is the worked example, and `local-dev/apps/openedx/Tiltfile` is
short enough to read end to end.

The pattern, and what makes it hold up over time:

1. **The owning repo exposes a parameterised entry point.** lehrer's
   `local-dev/lehrer-core.star` exports `setup(cfg)`; this repo pulls it in with
   `load_dynamic()` and passes topology. No manifests are copied. It has to be
   `load_dynamic()` rather than `load()`, because the path depends on
   `MITOL_WORKSPACE_ROOT` and Starlark's `load()` takes a string literal only.
2. **Say which shared services to reuse.** `manage_infra: False` tells lehrer
   not to install Valkey/OpenSearch because `local-infra` already has them.
   Anything this cluster genuinely lacks (MariaDB, MongoDB) stays the owning
   repo's to install.
3. **Layer config deltas; never fork a ConfigMap.** lehrer's manifests read an
   optional `*-config-overrides` ConfigMap last in `envFrom`, so this repo
   supplies only the keys that differ and inherits every key added upstream. A
   full replacement ConfigMap would silently go stale instead.
4. **Share credential defaults through a file both sides read**, rather than
   restating them. lehrer's `local-dev/secret-defaults.yaml` is read both by
   its CLI and by `setup()`'s `manage_secrets`, so the Secret is identical
   however the stack is started.

Steps 3 and 4 are what keep composition from decaying into a fork. If the
stack you are composing offers neither, adding them upstream is usually a
smaller change than maintaining the copy.

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

**Add a new shared service:** Add a module under `infra/modules/` and call it from `infra/core/__main__.py`. Use the existing modules (`cache.py`, `search.py`, `ai.py`) as references.

**Modify the Keycloak realm or add a new OIDC client:** Edit `infra/modules/keycloak.py`. On `pulumi up`, pulumi-keycloak will diff the realm state and apply only what changed.
