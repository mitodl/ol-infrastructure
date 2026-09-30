# Local-dev parity with the deployed architecture

`local-dev/` (k3d + Tilt) and the deployed environments (`src/ol_infrastructure/`)
describe the same applications twice. The platform layer is a separate Pulumi
codebase and the application layer is hand-written YAML. Two imports cross from
`local-dev/` into `src/`: `QDRANT_VERSION` (`local-dev/infra/modules/search.py:15`)
and the Keycloak organization flow builders
(`local-dev/infra/modules/keycloak.py:22-25`). Everything else is transcribed by
hand and has drifted.

This document inventories that drift and proposes how to close it under two
requirements:

1. Local-dev matches the deployed architecture closely enough that behaviour
   seen locally predicts behaviour in RC and Production.
2. Local-dev is composable, so a developer working on one app runs that app and
   what it needs, not the whole service mesh.

It extends the narrower question raised on
https://github.com/mitodl/ol-infrastructure/pull/5862 (whether `OLApplicationK8s`
can produce the local app manifests) to the whole stack.

## What has drifted

### Platform services

| Service | Local | Deployed | Shared code |
|---|---|---|---|
| APISIX | chart 2.17.0 (APISIX 3.18.0, controller chart 1.3.0) | chart 2.17.0 (APISIX 3.18.0, controller chart 1.3.0) | version pin |
| Gateway API | no `GatewayClass` or `Gateway` | `GatewayClass`/`Gateway` `apisix`, CRDs v1.6.2 | none |
| cert-manager | v1.21.2, installed, used by nothing | v1.21.2, `ClusterIssuer` per environment | version pin |
| Keycloak operator | 26.7.4, CR `v2beta1`, behind APISIX over HTTP | 26.7.4, CR `v2beta1`, behind Traefik over HTTPS | version pin, CRD list helper |
| Keycloak realm | 861 lines, credentials in k8s Secrets | about 1800 lines, credentials in Vault | flow builders |
| Postgres | CloudNativePG 1.25.0, one shared cluster, version unpinned | RDS per app, major 15 (mit_learn, mitxonline) or 18 | none |
| Valkey | `7.2.14-alpine`, no TLS, no auth, shared | ElastiCache Valkey 7.2, TLS and auth token, per app | none |
| OpenSearch | chart 3.4.0, security plugin off | AWS OpenSearch 3.3 | none |
| Qdrant | v1.19.1, one pod, no API key | Qdrant Cloud v1.19.1, API key | version pin |
| Tika | image from chart 3.2.2 (`3.2.2.0-full`), no auth | chart 3.2.2, access token, behind APISIX | version pin |
| Object storage | RustFS 1.0.0-rc.6, ocw-studio only | S3, IAM credentials issued by Vault | none |
| Secrets | committed plain Secrets | Vault and Vault Secrets Operator 1.6.0 | none |
| Observability | Loki, Alloy v1.7.5, Grafana, logs only | k8s-monitoring 4.5.2 to Grafana Cloud | none |
| nginx sidecar | 1.25 and 1.27 in four apps | removed from all five counterparts | none |

Every local-dev version now comes from `src/bridge/lib/versions.py` (step 2 of
the order of work below), so Renovate moves both columns.

Three of these change behaviour an application developer depends on.

- APISIX 3.15 vs 3.18. `src/ol_infrastructure/components/services/apisix.py:604-612`
  records that the nested `session.cookie.*` form is a silent no-op from 3.17.
  Local mitxonline routes use the nested form
  (`local-dev/apps/mitxonline/apisix-routes.yaml:90-94`) and deployed routes use
  the flat form. A session cookie change tested locally exercises a code path
  that does not exist in RC.
- No `Gateway` locally. `OLApisixHTTPRoute` attaches to the `apisix` Gateway
  (`src/ol_infrastructure/components/services/apisix_gateway_api.py:220-221`),
  so apps already migrated to Gateway API (ocw_studio, xpro) cannot use their
  deployed route definitions locally. ADR 0002 makes that the direction for
  every app.
- Redis without TLS or auth. Deployed apps connect with `rediss://` and a
  token. That connection handling is never run locally.

### Applications

Counts are environment variable names, deployed side taken as the union across
stacks.

| App | Local | Deployed | Shared | Local only | Deployed only | Same value in both |
|---|---|---|---|---|---|---|
| mit-learn | 104 | 207 | 92 | 12 | 115 | 37 |
| mit-learn-nextjs | 12 | 48 | 9 | 3 | 39 | 4 |
| learn-ai | 34 | 82 | 13 | 21 | 69 | 0 |
| mitxonline | 50 | 144 | 36 | 14 | 108 | 11 |
| ocw-studio | 115 | 145 | 75 | 40 | 70 | 9 |
| odl-video-service | 53 | 87 | 39 | 14 | 48 | 14 |

The last column is the maintenance cost: values typed twice. Some have already
diverged with no environment reason, e.g. `MITX_ONLINE_COURSES_API_URL` in
mit-learn (`/api/v2/courses/` locally, `/api/internal/courses/` in QA and
Production), `OCW_MASS_BUILD_BATCH_SIZE` in ocw-studio (80 vs 160), and
`ET_PRESET_IDS` in odl-video-service (one ID vs six).

Workload shape differs in every app:

- Names. The component creates `<app>-app` with container `<app>-app`; local
  uses `<x>-webapp` with container `app`.
- Ports. Deployed Granian listens on 8073 with metrics on 9090. Local uses a
  different port per app behind an nginx sidecar that deployed no longer has.
- Celery topology. Deployed mitxonline runs one Deployment per queue; local
  runs one Deployment consuming both. ocw-studio local runs three queues and
  beat in a single process.
- Startup. Local runs migrations in an init container with extra steps
  (`createcachetable`, `configure_wagtail`); deployed runs a pre-deploy Job.
- Routes. Local route YAML is a transcription of the deployed `OLApisixRoute`
  calls. Deployed routes carry `request-id`, 60s timeouts, the OIDC
  pre-function, stale session cookie cleanup, and per-route CSP headers that
  local routes lack. OIDC settings differ in scope, client auth method,
  `ssl_verify`, cookie name and cookie domain.

mit-learn-nextjs does not use `OLApplicationK8s` when deployed (raw Deployment
plus `OLEKSGateway` on Traefik, public host served by Fastly). Open edX local
manifests live in the lehrer repo, not here.

### Composability

`enabled_apps` selects which app Tiltfiles load. It gates almost nothing in the
platform layer: only RustFS, the `ocw-studio` namespace, and the ocw-studio
Keycloak client. With `enabled_apps: ["mitxonline"]` the cluster still runs
OpenSearch, Qdrant, Tika and LiteLLM, which mitxonline never references. By
configured memory limits that is about 5Gi of unneeded services against about
4Gi of needed ones (limits, not measured usage).

`local-dev/EXTENDING.md:43-46` advises against gating the four original apps,
because it would churn Pulumi state on every existing cluster. It also advises
against creating an optional app's database through CNPG `postInitSQL`, which
only runs at initdb and is immutable afterwards.

## What parity should mean

Not everything can or should match. I propose four tiers, and that each
platform module declares which tier it is in.

1. Same code. The local program calls the function or component the deployed
   stack calls, with different inputs. Applies to anything running in-cluster
   on both sides: APISIX and its Gateway, cert-manager, Keycloak, the realm,
   application workloads, routes.
2. Same interface, different implementation. A managed AWS service replaced by
   an in-cluster equivalent that speaks the same protocol at the same version
   with the same security posture. Postgres, Valkey, OpenSearch, S3, Qdrant.
   Parity here means pinning the version the app is deployed against and
   turning on TLS and auth.
3. Local only, by design. Mailpit, LiteLLM, self-hosted Loki and Grafana, the
   zot registry, the CoreDNS override, mkcert.
4. Absent, by design. HPA, KEDA, VPA, PodMonitor, `SecurityGroupPolicy`,
   external-dns, Karpenter, Fastly. These size or expose workloads and do not
   change what application code sees.

Where parity and the current local shape conflict, local changes to match
deployed. The component should not grow options to reproduce local-only
choices (per-app ports, the nginx sidecar). The exceptions are what a
development loop needs: reload flags on Granian, a CA bundle for mkcert, lower
resource requests, and a single celery worker by default (see "Celery
topology" below).

## Proposed design

### 1. One version source

Every local module reads its version from `bridge.lib.versions`. This has no
dependency on the rest of the design and removes the most mechanical drift.
Where local must lag (e.g. an operator upgrade that needs a migration), the
module pins an override next to a comment saying why.

### 2. Application definitions separated from environment bindings

Deployed application programs are single scripts. `mitxonline/__main__.py` is
1464 lines that interleave RDS, ElastiCache, S3, IAM, Vault and Fastly
resources with the environment dict, the `OLApplicationK8sConfig`, and the
route definitions. A local program cannot import any of it without AWS and
Vault providers.

Each application gets a definition module holding what is true in every
environment, and takes the rest as an argument:

```python
# src/ol_infrastructure/applications/mitxonline/definition.py
class MitxonlineBindings(BaseModel):
    hostnames: ...  # public hosts and cookie domain
    database: ...  # host, name, credential source
    cache: ...  # host, TLS, credential source
    object_storage: ...  # bucket, endpoint, credential source
    keycloak: ...  # base URL, realm, client
    secret_names: list[str]  # Secrets mounted with envFrom
    cluster: ClusterCapabilities


def application_config(bindings: MitxonlineBindings) -> OLApplicationK8sConfig: ...
def routes(bindings: MitxonlineBindings) -> list[OLApisixRouteConfig]: ...
```

The deployed `__main__.py` builds bindings from the AWS resources it creates.
A local program builds them from the local platform. Environment-invariant
settings (the 37 duplicated mit-learn values, celery queue names, probe paths,
Granian settings, route rules and priorities) exist once.

`ClusterCapabilities` carries what the component needs to know about the
cluster it renders for. The component change on this branch adds the knobs:

| EKS dependency | Knob | Local value |
|---|---|---|
| ECR image rewrite (app and nginx) | `registry` | `"direct"` |
| `SecurityGroupPolicy` and pod label | `application_security_group_id`, `application_security_group_name` | unset |
| Vault auth name (required, never read) | `vault_k8s_resource_auth_name` | unset |
| Webapp HPA or KEDA `ScaledObject` | `manage_webapp_autoscaler` | `False` |
| Celery KEDA `ScaledObject` per worker | `manage_celery_autoscalers` | `False` |
| `PodMonitor` | `manage_pod_monitor` | `False` |
| Webapp memory VPA | `manage_webapp_memory_vpa` (existing) | `False` |

Defaults are unchanged. See "Verification of the component change" below.

Importing the component used to call AWS before any knob was read: the
modules it imports created boto3 EKS and EC2 clients and looked up the account
ID at module level. Those are now created on first use
(`lib/aws/eks_helper.py`, `lib/aws/ec2_helper.py`, `lib/aws/aws_helper.py`),
so a machine with no AWS configuration can load and render the component.

Still missing from the component for local use, to be added as they are needed:

- Granian `--reload`, `--reload-ignore-dirs` and `--workers-kill-timeout`.
  `GranianConfig` has no field for them.
- Per-developer env overrides. The component writes inline `env`, and a
  ConfigMap mounted with `envFrom` cannot override inline `env`. The local
  program merges the developer's override file into the dict before handing it
  to the component.

### 3. Secrets

Deployed secret names are defined inside VSO Go templates
(`applications/<app>/k8s_secrets.py`). mit_learn has 34 secret-backed variable
names across 13 `VaultStaticSecret` and `VaultDynamicSecret` resources. Local
has its own list in `secrets.yaml`.

Decided 2026-09-28: secrets are described as data, and there is no Vault in
the local cluster. The two options that were considered:

- Run Vault (dev mode) and VSO in the local cluster, seeded from a committed
  file of dummy values. `k8s_secrets.py` and `OLApisixOIDCResources` then run
  unchanged. Dynamic database credentials work against CNPG through Vault's
  database engine. The AWS secrets engine has no local equivalent, so
  S3 credentials become KV entries. Cost is two more pods and a Vault to seed
  and unseal on every cluster start.
- Describe each secret as data (variable name, Vault mount, path, key), and
  generate the VSO template from it for deployed and a plain Secret from it
  for local. No Vault locally. Cost is rewriting every `k8s_secrets.py`, and
  the local path never exercises VSO.

The second was chosen. It removes the duplication without adding a stateful
service to every developer's laptop, and VSO behaviour (refresh, rollout
restart) is operations territory that local-dev does not need to reproduce.

`OLApisixOIDCResources` needs the same treatment: it creates an
`OLVaultK8SSecret` unconditionally (`apisix.py:586-602`) and must accept a
plain Secret as its credential source.

### Celery topology

Decided 2026-09-28: local-dev runs one celery worker Deployment consuming
every queue by default, and a developer can switch an app to the deployed
topology (one Deployment per queue).

The application definition lists its queues once, with the resources each
deployed worker gets. The deployed program turns that list into one
`OLApplicationK8sCeleryWorkerConfig` per queue, as today. The local program
reads a per-app setting:

- `merged` (default): one worker config with `worker_name="all"` and
  `queue_name` set to the comma-joined queue list, which celery's `-Q`
  accepts. `worker_name` has to be set explicitly because it becomes part of
  the Deployment name and a label value, where a comma is not valid.
- `per-queue`: the same configs the deployed program builds.

This needs no component change. `worker_name` and `queue_name` are already
separate fields. Beat stays its own Deployment in both modes, as deployed, so
the schedule never runs inside a worker that a developer restarts.

What the merged mode does not reproduce: a task routed to a queue that no
deployed worker consumes runs locally and hangs in RC, and per-queue memory
limits are not exercised. `per-queue` exists for checking those before a
release.

### 4. Platform layer built from deployed code

In dependency order:

- APISIX. Extract the Helm values from `setup_apisix`
  (`infrastructure/aws/eks/apisix_official.py`) into a function both callers
  use. Local passes 1 replica, NodePort, no HPA or VPA. Local gains the
  `GatewayClass`, `Gateway`, the `identity-header-strip` global rule, and
  `pluginAttrs.redirect.https_port`. Install Gateway API CRDs at
  `GATEWAY_API_VERSION`.
- cert-manager. Add a CA `ClusterIssuer` backed by the mkcert root and let
  `OLCertManagerCert` take the issuer name. Apps then request certificates
  the way they do deployed, and the wildcard Secret copied into every
  namespace goes away.
- Keycloak. Same operator version and CR apiVersion. Parametrize
  `create_olapps_realm` so the local realm is the deployed realm with Mailpit
  SMTP and local hostnames, and retire `local-dev/infra/modules/keycloak.py`.
  Decided 2026-09-28: Keycloak stays behind APISIX locally, served over HTTPS
  with a certificate from the CA issuer. Deployed Keycloak is behind Traefik,
  but running Traefik locally for one service costs more than it buys; what
  applications see (an HTTPS issuer URL on the Keycloak hostname) matches.
- Postgres, Valkey, OpenSearch. Pin to the deployed versions. Enable TLS and
  auth on Valkey and the security plugin on OpenSearch.
- Object storage. Decided 2026-09-28: RustFS, already installed for
  ocw-studio, becomes the `object-storage` capability. Each app stack that
  uses S3 when deployed (mit-learn, mitxonline, odl-video-service,
  ocw-studio) creates its own bucket and a static key Secret, and its
  bindings carry the RustFS endpoint.

### 5. Composition

Replace the two platform stacks with a catalog. Each entry declares what it
provides and what it requires:

```python
APPS = {
    "mitxonline": App(
        requires=["ingress", "identity", "postgres", "cache", "mail"],
        uses=["mit-learn", "openedx"],
    ),
    "mit-learn": App(
        requires=[
            "ingress",
            "identity",
            "postgres",
            "cache",
            "mail",
            "search",
            "vector-search",
            "tika",
            "llm-proxy",
        ],
        uses=["mitxonline", "learn-ai"],
    ),
}
```

- `requires` are platform capabilities. The set deployed is the union over
  enabled apps. mitxonline alone no longer starts OpenSearch, Qdrant, Tika or
  LiteLLM.
- `uses` are other applications. When the other app is enabled locally its
  binding points at the local Service. When it is not, the binding points at
  the RC deployment, or is left unset where the app tolerates that.
  ocw-studio already does this by hand for `LEARN_AI_SYLLABUS_ENDPOINT`
  (`local-dev/apps/ocw-studio/configmaps/app-env.yaml:78`).
- Each application is its own Pulumi stack that owns its namespace, database,
  Keycloak client, Secrets, workloads and routes. This is the shape of the
  deployed projects, and it is what makes an app removable: disabling it
  destroys one stack.
- Databases move from `postInitSQL` to the CNPG `Database` resource (available
  in the operator version already installed, 1.25.0), owned by the app stack.
  That removes the `postInitSQL` limitation described in `EXTENDING.md`.
- Named profiles in `tilt_config.json` cover the common cases, e.g.
  `learn-frontend` (mit-learn-nextjs against the RC API) and `mitxonline`.

Existing clusters carry state for the unconditional resources. Moving them
between stacks needs either a one-time `teardown.sh` and rebuild or Pulumi
aliases. A rebuild is simpler and local data is disposable, so I propose
announcing a rebuild.

### 6. Tilt integration

The two options from #5862 stand. Facts established since:

- `k8s_custom_deploy` accepts `image_deps`, `live_update`, and
  `container_selector`, and passes image references to the apply command as
  `TILT_IMAGE_<n>` (https://docs.tilt.dev/api.html). Tilt documents live
  update for Option 1; it has not been tried here.
- `renderYamlToDirectory` is still labelled beta in pulumi-kubernetes 4.34.1,
  the version in `uv.lock`.
- Under the per-app stack design, Pulumi already applies the app's namespace,
  database, Keycloak client and Secrets. Option 2 would split one app between
  two apply paths.

Option 1 is the working choice as of 2026-09-28, to be confirmed by a number
from the mitxonline pilot. What is not known is the cost of `pulumi up`. No
other app is converted until the pilot reports it.

The pilot measures wall-clock time of the apply command, from Tilt invoking it
to it printing YAML, against the time Tilt takes today to apply the
hand-written mitxonline manifests for the same trigger:

- first apply into an empty namespace (the cost each app adds to `tilt up`)
- a no-op apply (the floor: program start, provider plugin load, state read)
- a one-variable env change
- an image rebuild, where only the image reference changes

Each is reported as the median of five runs, with the machine and Pulumi
version stated. Pod readiness is excluded from both sides, since it is the
same pod either way. The pilot also reports whether live update works through
`k8s_custom_deploy`, because if it does not, Option 1 is out regardless of the
timings.

If the no-op floor turns out to dominate, the cheaper fixes are tried before
falling back to Option 2: `--skip-preview`, a local file backend (already the
case for the platform stacks), and keeping the provider plugin warm.

## Verification of the component change

The acceptance test written on the task was `pulumi preview` showing no
changes on every stack. That signal is not available: deployed stacks carry
pending changes unrelated to this branch (e.g. `ol_analytics_api` CI shows
three updates on `main`). Two checks replace it.

- Under Pulumi mocks, three configurations covering every optional feature
  register 35 resources. The inputs are byte-identical before and after the
  change.
- For each stack, `pulumi preview --json` is run from `main` and from this
  branch with the same inputs, and the planned steps are compared (operation,
  property diff, inputs, parent).

Result of the second check on 2026-09-28, against `main` at 2e9f9e609, over
74 stack files: the 11 projects that use the component (edxapp, edx_notes,
learn_ai, micromasters, mit_learn, mitxonline, ocw_studio, odl_video_service,
ol_analytics_api, xpro, xqueue), plus `infrastructure/aws/eks` and
`infrastructure/aws/network`, which call the AWS helpers that changed.

- 68 stacks plan identical steps.
- 6 stack files have no state to preview against (ocw_studio Dev; network Dev;
  xqueue mitx.CI, mitx-staging.CI, mitxonline.CI, mitxonline.QA).

Names that Pulumi generates are masked in the comparison. Four xqueue stacks
have never been applied and their HPA has no explicit name, so its random
suffix differs between any two previews, including two from `main`.

Local behaviour is covered by `test_k8s_local_cluster.py` and
`test_k8s_without_aws.py` in `tests/ol_infrastructure/components/services/`. It
has not been applied to a k3d cluster; that happens with the first local
program.

## Order of work

1. Component knobs (this branch).
2. Versions from `bridge.lib.versions`.
3. mitxonline as the pilot: definition module, secrets as data, local program,
   Option 1 Tilt wiring, `pulumi up` latency measured. mitxonline is the
   smallest app with gateway OIDC, celery, and a dependency on another app.
4. Platform parity: APISIX and Gateway, cert-manager issuer, Keycloak and
   realm, data service versions and TLS.
5. Catalog, per-app stacks, profiles.
6. Remaining apps: learn-ai, mit-learn, odl-video-service, ocw-studio.
7. mit-learn-nextjs and Open edX (see "Decisions" below).

Steps 2 and 4 do not depend on step 3 and can run in parallel with it.

## Decisions

Made by the devops lead on 2026-09-28, in addition to those recorded inline
above (secrets as data, celery topology, Tilt Option 1 pending the pilot).

- Keycloak ingress. Behind APISIX over HTTPS locally, not Traefik. See
  "Platform layer" above.
- mit-learn-nextjs. Its env construction is extracted into a definition
  module that the deployed program and the local program both call. The
  workload stays a raw Deployment, so production is not renamed and the
  component does not need per-key secret env or a configurable `PORT`.
- Open edX. In scope. We maintain lehrer, and the lehrer side is tracked in
  the "lehrer: local-dev DX and repo hygiene" project. The existing boundary
  holds: lehrer owns build definitions, ol-infrastructure owns deployment
  topology. The deployed edxapp celery workers are outside `OLApplicationK8s`,
  so the edxapp definition module covers both the component and those
  workers.
- Object storage. RustFS with a bucket per app. See "Platform layer" above.
- Autoscaling. KEDA is out of scope for local-dev, so there is no fidelity
  profile. Autoscaling and scrape configuration are tested in RC.

## Found along the way

Not part of this work. Each needs its own check before acting on it.

- `mit_learn/Pulumi.QA.yaml:46` sets `mit_learn:min_replicas`; the program
  reads the `mitlearn` namespace (`mit_learn/__main__.py:2013`), so QA falls
  back to 2. Confirmed in code on 2026-09-28; Production uses the right key.
- odl_video_service's broker URL uses Redis database 0
  (`odl_video_service/k8s_secrets.py:168`) while its KEDA trigger reads the
  worker config default, database 1. Read from code, not observed.
- `prebuilt_tags` in `tilt_config.json` is consumed by nothing; every local
  manifest pins `:latest`. `local-dev/README.md` documents it as working.
- `local-dev/scripts/setup.sh:280-284` recommends 8 GB for the full stack;
  `local-dev/README.md:62` says 12 to 16 GB.
