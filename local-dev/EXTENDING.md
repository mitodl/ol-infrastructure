# Extending the Local Development Environment

How to add a new app to the stack or change the shared in-cluster infrastructure. For day-to-day usage see the [README](README.md); for how the pieces fit together see [ARCHITECTURE.md](ARCHITECTURE.md).

## Table of Contents

1. [Adding a New App](#adding-a-new-app)
2. [Composing a Stack from Another Repo](#composing-a-stack-from-another-repo)
3. [Modifying Shared Infrastructure](#modifying-shared-infrastructure)

## Adding a New App

### Which surface?

Pick one:

- **The app has a deployed definition** (`src/ol_infrastructure/applications/<app>/definition.py`): a Pulumi program under `local-dev/apps/<app>/pulumi/` renders it with k3d bindings, and Tilt applies it through `local-dev/scripts/pulumi-app-apply.sh` as a `k8s_custom_deploy`. mitxonline is the example: its `pulumi/` and Tiltfile are the template. A developer's env and memory limits for such an app live in its gitignored stack file (`pulumi/Pulumi.Dev.yaml`; `vars` and `web_memory_limit` are the deployed stacks' keys, the other limits local ones in the same style; mitxonline ships a `.example`), not in an `app-env.local.yaml`. Two touchpoints sit outside the app directory: the root `Tiltfile` raises the apply timeout per Pulumi-rendered app, and `teardown.sh` destroys each program's stack by name.
- **No deployed definition** (local-only services, apps not yet on `OLApplicationK8s`): Kubernetes manifests under `local-dev/apps/<app>/`, as step 1 below describes.
- **Shared infrastructure several apps use** (a database engine, a cache, an identity provider): a module under `local-dev/infra/modules/`, called from the `core` stack; see [Modifying Shared Infrastructure](#modifying-shared-infrastructure).

Either kind of app also needs its prerequisites from the `apps_infra` stack: the database (step 2), the namespace and TLS Secret (step 3) and the Keycloak client (step 4). Steps 2 to 6 apply to both kinds.

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
("CREATE DATABASE myapp OWNER app;",)
```

### 3. Register the namespace (TLS secret comes with it)

Add `"my-app"` to the `APP_NAMESPACES` tuple in `local-dev/infra/modules/namespaces.py`. `tls.py` reads the same list through `app_namespaces_for()`, so the `local-dev-tls` Secret, the mkcert CA ConfigMap and the `local-dev-ca-bundle` ConfigMap appear in the new namespace automatically.

If the app calls local hostnames over HTTPS, mount `local-dev-ca-bundle` as a directory at `/etc/ssl/local-dev` and set `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` to `/etc/ssl/local-dev/ca-certificates.crt` (or `NODE_EXTRA_CA_CERTS` for Node). In a Pulumi program that is an entry in `extra_volumes` and `extra_volume_mounts`, as in `local-dev/apps/mitxonline/pulumi/__main__.py`. Don't copy the `ca-bundle-merge` init container from the older manifests; see "Trusting local TLS from pods" in [ARCHITECTURE.md](ARCHITECTURE.md#design-decisions).

If the app brings heavyweight infrastructure with it — an object store, a CI system, anything a developer who is not working on that app should not be paying for — or is one most developers will not run (`micromasters`), put its name in `OPTIONAL_APP_NAMESPACES` instead. Namespaces listed there are only created when the app appears in `enabled_apps`, which the Tiltfile forwards to both Pulumi stacks as `LOCAL_DEV_ENABLED_APPS`. Gate the expensive resources on the same value; `ocw-studio` and the RustFS object store are the worked example. `data-platform` (`local-dev/infra/modules/lakehouse.py`) is the example of an `enabled_apps` entry that is infrastructure only, with no namespace or app Tiltfile of its own. An app Tiltfile can read the same variable from `os.environ` to change its own manifests when another app is enabled; `local-dev/apps/mit-learn/Tiltfile` does this for `data-platform`.

Two things not to do:

- Don't add the app's database to the CNPG cluster's `postInitSQL` (step 2) if the app is optional. That block only runs when initdb bootstraps an empty data directory, so it does nothing for anyone who already has a cluster, while still producing a spec diff Pulumi will try to apply to an immutable `initdb` section. Create the database from an init container instead — `local-dev/apps/ocw-studio/deployment.yaml` shows the idempotent pattern.
- Don't retrofit gating onto the four original apps. Their namespaces and databases are provisioned unconditionally on purpose: changing that would churn Pulumi state on every existing developer's cluster to save a namespace and an empty database.

### 4. Add the Keycloak OIDC client (if needed)

In `local-dev/infra/modules/keycloak.py`, add a new client and call `_make_oidc_secret()` to create the OIDC credentials Secret in the app namespace.

### 5. Register in the root Tiltfile

In `Tiltfile`, add an entry to the `APPS` list:

```python
(
    {
        "name": "my-app",
        "dir": "my-app",  # sibling repo directory name
        "namespace": "my-app",
        "deploy_name": "myapp-webapp",
        # prebuilt_tags key -> published image; see "Editing code" in the README.
        "images": {"my-app": "mitodl/my-app"},
        "tiltfile": "./local-dev/apps/my-app/Tiltfile",
        "seed_commands": [
            {
                "label": "seed-myapp-data",
                "description": "Load initial data",
                "cmd": "python manage.py loaddata initial_data",
            },
        ],
    },
)
```

Each seed becomes a manual Tilt resource that runs `cmd` in the deployment's pod through `kubectl exec`. Two optional keys cover a seed that needs something from the host first (`seed-mitxonline-instance` is the example):

- `host_env`: `{"NAME": "<host command>"}`. Each command runs on the host before `kubectl exec`; a non-zero exit stops the seed, and its output reaches `cmd` as the environment variable `NAME`.
- `resource_deps`: Tilt resources to wait for besides the app's deployment, e.g. `["kc-seed-users"]`.

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

**Change a chart, operator or image version:** Versions live in `src/bridge/lib/versions.py`, and Renovate bumps them. Where a version is shared with a deployed stack, local-dev follows it. If local-dev has to lag (e.g. an upgrade that needs a migration), pin the older value in the module that uses it, with a comment saying why, rather than changing the shared constant.

**Add a new shared service:** Add a module under `infra/modules/` and call it from `infra/core/__main__.py`. Use the existing modules (`cache.py`, `search.py`, `ai.py`) as references.

**Modify the Keycloak realm or add a new OIDC client:** Edit `infra/modules/keycloak.py`. On `pulumi up`, pulumi-keycloak will diff the realm state and apply only what changed.
