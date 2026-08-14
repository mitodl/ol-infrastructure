# Extending the Local Development Environment

How to add a new app to the stack or change the shared in-cluster infrastructure. For day-to-day usage see the [README](README.md); for how the pieces fit together see [ARCHITECTURE.md](ARCHITECTURE.md).

## Table of Contents

1. [Adding a New App](#adding-a-new-app)
2. [Modifying Shared Infrastructure](#modifying-shared-infrastructure)

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

The client secret itself lives in three places that must be kept in sync by hand — there is no output plumbing between the Pulumi stacks and the app manifests:

1. `local-dev/infra/apps_infra/Pulumi.local-dev.apps-infra.Dev.yaml` — the plaintext local-only value
2. `local-dev/infra/apps_infra/__main__.py` — `config.require_secret(...)`, passed into `create_olapps_dev_realm()`
3. the app's `secrets.yaml` — the same literal, under whatever env var the app reads

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
