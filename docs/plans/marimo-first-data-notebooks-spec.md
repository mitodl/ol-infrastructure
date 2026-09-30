# Spec: marimo-first data notebooks and in-cluster publishing

Status: spec, ready for review
Date: 2026-09-18
Project: `wp-marimo-first-data-notebooks-default-marimo-ux-fa-081f81`
Predecessor: `wp-marimo-jupyterhub-notebook-onboarding-auth-fix-t-c5925f` (templates, Galaxy
OAuth2 auth, postStart seeding; completed 2026-09-10)

## Scope

`nb.data.ol.mit.edu` (`applications/jupyterhub_data`) should be a marimo environment. Today it
isn't one. Users land in JupyterLab and `.py` files open in the text editor. The first open of a
notebook is slow and often times out with no feedback. There is also no way to publish a notebook
as an app.

This spec covers two epics:

- Part A makes the interactive environment marimo-first: the default viewer, a fast cold start,
  and versioning through GitHub.
- Part B adds a low-step path from a notebook in JupyterHub to a published marimo-operator app
  on its own URL. Each app is either Keycloak-gated or public, and going public needs an
  approval.

The first implementation pass ships Keycloak-gated apps only (D11). Public apps are still
designed here, under "Pass 2: public apps", so that nothing in pass 1 rules them out.

Out of scope: the StarRocks migration itself (`wp-trino-starburst-galaxy-starrocks-migration-decom-62b677`
E7 owns warehouse connectivity), and the org-wide APISIX OIDC trust boundary
(`wp-apisix-owned-oidc-trust-boundary-org-wide-securi-0409aa`). The publisher is designed to stay
out of the latter.

## Decisions

| # | Decision |
|---|---|
| D1 | Publishing goes through an in-cluster publisher service. Versioning goes through GitHub, from inside JupyterHub. Public access needs review and approval. |
| D2 | One apps host per environment, one path per notebook: `apps.nb.data.ol.mit.edu/<name>/` (CI/QA: `apps.nb-ci.data.ol.mit.edu`, `apps.nb-qa.data.ol.mit.edu`). One cert, one DNS entry, and one wildcard Keycloak redirect URI (`https://<apps host>/*`) per environment, because APISIX derives `redirect_uri` from the request path. |
| D3 | The landing page stays JupyterLab, with marimo as the default viewer for `.py`. |
| D4 | Public notebooks query StarRocks as a new, restricted `notebook_public` role. Gated notebooks use the existing `readonly` role. |
| D5 | The publisher is registered as a JupyterHub service and authenticates callers with hub tokens or hub OAuth. It never reads `X-Userinfo`. |
| D6 | The MarimoNotebook and ApisixRoute CRs are the publisher's only state. There is no database. |
| D7 | Published apps keep marimo token auth (`auth.password`, a per-app Secret). APISIX injects the token upstream with `proxy-rewrite`, so APISIX is the only entry point for viewers. Inside the cluster, anything that holds an app's token can still reach that app's Service directly: the app itself and APISIX. The token matters because NetworkPolicy is not enforced on the data cluster (F16). |
| D8 | Warehouse credentials reach published pods as mounted files, not env vars. |
| D9 | Published routes never forward viewer credentials. The OIDC plugin sets `set_access_token_header`, `set_id_token_header`, and `set_userinfo_header` to `false` (F17). Turning those off only stops APISIX from adding the headers, and a client can still send its own, so `proxy-rewrite` also removes `Cookie`, `X-Access-Token`, `X-ID-Token`, and `X-Userinfo`, and overwrites `Authorization` with the app token. Notebook code must not use any of these headers for identity. |
| D10 | Approving (pass 2) is gated on a Keycloak role, synced into JupyterHub groups (`manage_groups`, `auth_state_groups_key`). The publisher checks group membership through the hub API with its service token (F18). |
| D11 | The first implementation pass ships Keycloak-gated apps only. The `notebook_public` role, the public route shape, and the approval workflow are deferred to pass 2. |
| D12 | In pass 1, anyone who can log in to the hub may publish. The publisher checks only that the caller holds a valid hub token. |

D1 through D4, D11, and D12 are the owner's decisions from 2026-09-18. D5 through D10 follow from
the facts below.

D12 is an accepted risk. The hub admits every `ol-data-platform` realm user (F18), and a gated
app's author can read the app's `readonly` credential (F14). In pass 1, then, any realm user can
obtain `readonly` by publishing an app. Revisit this if hub access widens, or before pass 2 adds
a second credential.

## Facts this rests on

Read from source on 2026-09-18. Refs: marimo-jupyter-extension `1587fe3` (v0.4.0), marimo-operator
`31bd9bc` (v0.3.0 plus 4 commits, none of which touch Go code), marimo `43c1089` (main),
ol-infrastructure `e615f6ed4`, ol-data-platform `main`.

**F1. The marimo widget factory already handles plain `.py`. Only the default is missing.**
`labextension/src/index.ts:969-973` registers factory `marimo` (`widget-factory.ts:53`) with
`fileTypes: ['marimo', 'python']` and `defaultFor: ['marimo']`. So JupyterLab's
`@jupyterlab/docmanager-extension:plugin` setting `defaultViewers: {"python": "marimo"}` makes a
double-click on any `.py` open in marimo, with no change to the extension.

**F2. The 120s proxy timeout only bounds marimo server start.** `__init__.py:31-65` launches
`<marimo_cmd> edit --sandbox --port {port} ...` under jupyter-server-proxy with
`"timeout": config.timeout`. `executable.py:34-36` makes `marimo_cmd` equal to
`[uvx, "marimo[sandbox]>=0.23.14"]` when `uvx_path` is set, which it is
(`jupyterhub_data/deployment.py:547-556`). So the 120s window covers uvx resolving and
installing marimo itself, into `UV_CACHE_DIR=/home/jovyan/.cache/uv` on EFS
(`deployment.py:581`). Each notebook's own venv gets built later, when its session opens, and a
different timeout applies there. A1 has to find out which timeout users actually hit.

**F3. Upstream marimo already solved the "silent spinner" part of A5, but it isn't released
yet.** marimo#10821 (merged 2026-09-16) makes notebook provisioning async, so the UI opens before
the kernel is ready. It also reports `StartupPhase = "preparing-environment" | "starting-kernel"`
(`marimo/_session/model.py:7`). marimo#10822 (merged 2026-09-16) adds the startup and recovery UI.
The kernel startup timeout is 600s and can be overridden with `MARIMO_KERNEL_STARTUP_TIMEOUT`
(`marimo/_session/managers/ipc.py:75-86`). The latest release is 0.24.2 (2026-09-11), which
predates both PRs.

**F4. On marimo main, each notebook sandbox is a uv script environment.**
`marimo/_environments/environment.py:208-221` runs
`uv sync --script <file> --compile-bytecode --output-format json`. uv keeps script environments
under its cache directory, so on marimo main `UV_CACHE_DIR` sets where the venvs live as well as
the wheels. `--compile-bytecode` adds a `.pyc` write per module. Both make an EFS-backed cache
more expensive. (That script environments live in the cache is uv's documented behavior. A1
should confirm it by listing `$UV_CACHE_DIR` on a live pod.)

**F5. The image floats everything.** `ol-data-platform/images/marimo-jupyterlab/Dockerfile`
uses `quay.io/jupyter/minimal-notebook:latest`, `ghcr.io/astral-sh/uv:latest`,
`marimo[sandbox]>=0.19.11`, and an unpinned `marimo-jupyter-extension`.
`src/bridge/lib/versions.py:100-104` deploys `MARIMO_JUPYTERLAB_VERSION = "latest"`.

**F6. marimo-operator passes no `--base-url`.** `pkg/resources/pod.go:155-199` builds
`[mode, --headless, --host=0.0.0.0, --port=N, <auth flags>, --sandbox <file>]`. There is no
base-URL field in the CRD, and no upstream issue or PR asks for one.

**F7. `podOverrides` can replace the operator's marimo args without a fork.** `applyPodOverrides`
(`pod.go:386-426`) is a strategic merge patch over the PodSpec. Containers merge by `name`, and
`args` is a plain list, so an override on container `marimo` replaces the args wholesale. That
makes `--base-url` a stopgap the publisher can own until upstream support lands. The cost is
coupling to operator internals: `NotebookDir=/home/marimo/notebooks` (`pod.go:20`), the content
filename from `DetectContentKey` (`configmap.go:54-66`, `notebook.py` for marimo Python), and the
port. Every error path in `applyPodOverrides` (`pod.go:390-424`) silently returns the unpatched
spec. A malformed override would drop `--base-url` and the credential volume without any error,
so the publisher has to read back the reconciled Pod and check its args, volumes, and
ServiceAccount. B1 checks the override on a live pod.

**F8. Updating `content` does not redeploy the app.** The controller recreates the Pod only when
the hash of `pod.Spec` changes (`pod.go:28-35`, `marimonotebook_controller.go:197-233`). A new
`content` updates the ConfigMap in place (`marimonotebook_controller.go:138-141`), but the pod
spec only references the ConfigMap by name, and the `copy-content` init container already copied
the old file into an emptyDir. The publisher has to put the content hash into the pod spec (e.g.
env `OL_NOTEBOOK_CONTENT_SHA256`) so an update forces a new pod. The upstream fix is to fold the
content hash into the spec hash.

**F9. The operator generates a token by default.** With `spec.auth` unset, marimo generates a
token (`pod.go:163-191`), so a public visitor would be asked for one. `auth: {}` means
`--no-token`, which leaves the Service open to anything that can reach it in-cluster (see F16).
`auth.password` mounts a Secret and passes `--token-password-file` (`pod.go:166-187`).
marimo-jupyter-extension already uses the same shape: a random token, with the proxy injecting
`Authorization: Basic` upstream (`__init__.py:67-70`). D7 follows that pattern.

**F10. A deleted Pod comes back.** The controller `Owns(&corev1.Pod{})`
(`marimonotebook_controller.go:332`) and creates the Pod on NotFound (`:210`). B1 still checks
this live.

**F11. The operator pins the runtime layout.** It sets `VIRTUAL_ENV=/opt/venv`,
`UV_PROJECT_ENVIRONMENT=/opt/venv`, `UV=/usr/bin/uv`, `UV_SYSTEM_PYTHON=1`, and a `PYTHONPATH`
hardcoded to `python3.13` site-packages (`pod.go:204-212`). A `setup-venv` init container runs
`uv venv /opt/venv` from `spec.image` (`pod.go:123-132`). The runtime image (B3) must therefore
be Python 3.13 with `uv` at `/usr/bin/uv`, or override these through `spec.env`.

**F12. The two stacks share a hostname.** `marimo_data:apps_domain` equals
`jupyterhub_data:domain` in CI, QA, and Production (`Pulumi.<env>.yaml:4` in both projects), and
both stacks create a Certificate and ApisixTls for it.

**F13. Only legacy ApisixRoute can carry OIDC.** `OLApisixRoute` supports `websocket`
(`components/services/apisix.py:303`, `:408`) and plugin `secretRef` (`:594`). The Gateway API
path serializes plugins for the v1alpha1 PluginConfig CRD, which has no `secretRef` field
(`components/services/apisix_gateway_api.py:325-338`). So published routes use `OLApisixRoute` and its
CRD shape. A public route is the same route without the OIDC plugin.

**F14. The StarRocks role pattern is in place.** Native roles `readonly`, `app`, and `admin`
own the privileges, and Vault dynamic users get `GRANT <role> TO USER`
(`substructure/starrocks/__main__.py:84-215`). `readonly` has SELECT on all tables in all
databases and on the external catalog (`:344-346`). Vault leases default to 3 months, with a
6-month max (`:70-71`). `notebook_public` is one more entry in
`starrocks_role_statements`, plus its grants in the roles-setup SQL.

**F15. The shared-notebooks work (A6) is already in review.** PR #5923
(`jupyterhub-data-shared-notebooks`, 3 commits).

**F16. NetworkPolicy is not enforced on the EKS clusters.** The VPC CNI addon is configured with
`enable_network_policy=False` (`infrastructure/aws/eks/__main__.py:403`), and the data clusters
are built from the same entrypoint. A NetworkPolicy object would be accepted and then ignored.
(This was read from IaC. The live addon config was not checked.)

**F17. A gated route hands viewer credentials to author code.** APISIX 3.17.0's openid-connect
plugin defaults `set_access_token_header`, `set_id_token_header`, and `set_userinfo_header` to
`true` (`apisix/plugins/openid-connect.lua:301-323`). `OLApisixOIDCResources` doesn't override
them (`components/services/apisix.py:566-575`). marimo exposes every non-marimo request header,
and all cookies, to notebook code through `mo.app_meta().request`
(`marimo/_runtime/commands.py:184-197`). Anyone who can publish a gated app could therefore
collect each viewer's Keycloak access token, ID token, and APISIX session cookie. With every app
on one host (D2), that cookie is valid for every other app there.

**F18. The hub admits every realm user and knows no roles.** `jupyterhub_data` sets
`c.Authenticator.allow_all = True` (`jupyterhub_data/deployment.py:88`) and configures no
`manage_groups` or groups key, so a hub token identifies a user but carries no role. OAuthenticator
syncs groups from `auth_state` when `manage_groups=True` and `auth_state_groups_key` is set
(`claim_groups_key` is deprecated since 17.0). The hub already runs with `enable_auth_state`.
Combined with F14, this means any realm user who can publish a gated app can read a 3-month
`readonly` credential from inside it.

## Design

### Part A: interactive environment (`jupyterhub_data`, `marimo-jupyterlab` image)

**A1. Measure first.** On a fresh pod and on a restarted pod, time the uvx resolve of marimo,
the per-notebook `uv sync --script` on EFS vs. an emptyDir, and marimo server start. Record which
timeout fires and what the user sees: jupyter-server-proxy's 120s (F2), the APISIX upstream
timeout, or the kernel websocket. List `$UV_CACHE_DIR` to confirm F4. A2 through A5 are judged
against these numbers.

**A2. Stop resolving marimo through uvx.** In the Dockerfile, pin the base image digest, uv,
`marimo[sandbox]`, and `marimo-jupyter-extension`. Replace `uvx_path` with `marimo_path` in
`deployment.py`, and pin `MARIMO_JUPYTERLAB_VERSION` to a git short-ref tag with a Renovate
annotation. Target the first marimo release that contains #10821 and #10822 (F3). Per-notebook
`--sandbox` stays.

**A3. A pre-warmed uv cache off EFS.** Build a uv cache into the image with the template
dependency set (from `uv sync --script` on each template). At runtime, point `UV_CACHE_DIR` at a
writable emptyDir seeded from the image cache. It can't be the image path itself: uv writes each
script environment into the cache (F4), so a read-only cache fails on any dependency the image
doesn't carry. A1 measures whether seeding (a copy, since the image layer and the emptyDir are
separate filesystems) costs less than the EFS writes it replaces. Per F4, the notebook venvs move
off EFS with it. The cost is
that non-template dependencies download again after each pod restart. A1 decides whether that
beats NFS writes.

**A4. marimo as the default viewer.** Add an `extraFiles` entry that mounts
`/opt/conda/share/jupyter/lab/settings/overrides.json` (path to be confirmed against the pinned
base image) with `{"@jupyterlab/docmanager-extension:plugin": {"defaultViewers": {"python":
"marimo"}}}` (F1). Set `singleuser.defaultUrl` to the generated landing notebook from A8,
`/lab/tree/notebooks/welcome.py`.
The extension already adds a marimo "New notebook" tile to the launcher's Notebook category
(`index.ts:944-949`), next to the ipykernel tile. Leaving the ipykernel tile in place needs no
work. Hiding it is a UX call for this task.

**A5. Startup progress.** Most of this is A2 picking up marimo#10821/#10822 (F3). What remains
here is raising whichever timeout A1 shows users hitting, and a follow-up upstream only if the
released UI still hides uv output during a long `preparing-environment` phase. Record the PEP 723
install-UX pattern in witan once it is settled.

**A6. Shared notebooks by link.** Land PR #5923 (F15).

**A7. GitHub versioning.** Add `jupyterlab-git` to the image. For per-user auth, prefer a GitHub
App user-to-server token stored in the EFS home. It only reaches repos where the App is
installed, while a `gh auth login` device-flow token reaches every repo the user can. Repository
convention: see Q2. The client sends the notebook's git remote and HEAD commit with a publish
request, and the publisher records them as provenance annotations (a remote contains `:` and
`/` and can exceed 63 characters, so it can't be a label value). They are asserted by the client and
not verified. The published `content` is what runs and what B8 reviews, and it can differ from
HEAD.

**A8. Template updates reach existing users.** `cp -n` never overwrites, so template fixes never
reach anyone who already has a copy. Seed into a versioned `~/notebooks/templates/<version>/`
directory. Write a new, generated `~/notebooks/welcome.py` on every start that links to the
newest template directory. Its header marks it as generated, so users don't treat it as theirs.
Existing `getting_started.py` copies stay user-owned and are never overwritten, as the current
postStart hook promises (`jupyterhub_data/deployment.py:554-574`).

### Part B: publishing (`marimo_data`, marimo-operator, publisher service)

**B1. Apps host and a hand-applied prototype.** Set `marimo_data:apps_domain` to the D2 hosts
(F12), add them to `eks:apisix_domains` in `infrastructure/aws/eks/Pulumi.data.<env>.yaml`, and
add the redirect URI to the Keycloak client behind `ol-apisix-marimo-data-oidc-secrets`. In QA,
hand-apply one MarimoNotebook (`mode: run`, inline `content`, `auth.password` from a Secret) and a
gated `OLApisixRoute` at `/<name>/*` with `websocket=True`, `unauth_action="auth"`, and the three
D9 header flags off. The route uses `proxy-rewrite` to inject the marimo token and strip `Cookie`
(D7, D9). The public route shape waits for pass 2. Check each of these on the live pod:

- the F7 args override serves correctly under the path prefix;
- `mo.app_meta().request` in the gated app shows no `Authorization`, `X-Access-Token`,
  `X-ID-Token`, `X-Userinfo`, or `Cookie` from the viewer (F17), including when the viewer sends
  those headers themselves (D9);
- a request to the app Service from another pod, without the token, is refused (F16);
- `proxy-rewrite` can take the token from the per-app Secret through the plugin `secretRef`,
  rather than holding it in plaintext in the ApisixRoute;
- an edit to `content` without the F8 env hash does not redeploy, and does with it;
- a deleted Pod comes back (F10);
- the gated route survives the known OIDC bugs (`tk-nb-learn-mit-edu-sends-an-http-not-https-oidc-re-30cc6c`,
  `tk-apisix-eliminate-oidc-state-mismatch-failures-78-e1dfb4`).

**B2. Operator base URL, upstream.** Open a marimo-operator PR adding `spec.baseUrl` and folding
the content hash into the pod spec hash (F6, F8). Until it is released, the publisher uses the F7
override and the F8 env hash, so this task does not block B5 once B1 has shown the stopgap works.
Fall back to a pinned fork only if upstream refuses.

**B3. Runtime image.** The same build as A2/A3, without JupyterLab: Python 3.13, uv at
`/usr/bin/uv` (F11), pinned marimo, a pre-warmed cache, and the `ol_notebook` helper package. The
operator runs `marimo run --sandbox`, so the environment is built once per pod start, from the
image cache.

**B4. Warehouse credentials.** A VSO `VaultDynamicSecret` in namespace `marimo` produces a
Secret from the existing `readonly` Vault role (F14). The publisher mounts it as a file volume
through `podOverrides` (D8). The operator creates bare Pods, which VSO's
rollout-restart cannot target, while a mounted Secret volume picks up rotation on its own.
`ol_notebook.warehouse.connect()` returns the per-user Keycloak JWT credential when running
interactively and reads the mounted file when published. Give the notebook credential a
short `default_ttl` (hours, not the 3-month default in F14), since VSO renews dynamic secrets
itself. Published pods share one `marimo-published` ServiceAccount with no IRSA annotation and
`automountServiceAccountToken: false`, never the hub's IRSA role (`tk-notebook-pods-share-one-irsa-role-so-direct-s3-g-293885`).
Connectivity comes from StarRocks E7 (`tk-marimo-published-apps-replace-the-ol-marimo-app--42582d`,
`tk-published-marimo-apps-will-401-on-galaxy-ol-mari-7a13b5`, `tk-open-fe-mysql-nlb-ingress-for-the-consumers-that-9088e0`).

**B5. Publisher service.** A Python service in `ol-data-platform`, image built by GHA, registered
under `hub.services` (D5).

- API: `publish`, `update`, `list` (mine), `status` (phase, recent events, pod logs), and
  `unpublish`.
- Authorization: any caller with a valid hub token may `publish` a new name (D12). `update`,
  `status`, and `unpublish` act only on apps whose owner label matches the caller. Pass 1 has no
  admin override, and a platform admin uses `kubectl`.
- Validation: a PEP 723 header, `marimo check` passes, and content under 900 KiB. The 1 MiB
  limit applies to the whole ConfigMap object, so the key and metadata need headroom. The name
  must be a DNS label that is unique or already owned by the caller, and not a reserved gateway
  path. Reserve at least `logout`, because the shared OIDC plugin uses `/logout/oidc`
  (`marimo_data/__main__.py:179`), and an app named `logout` would capture it.
- Renders a MarimoNotebook with `mode: run`, inline `content`, `auth.password` pointing at a
  per-app token Secret (D7), the B3 image, the F7 args override, the F8 content hash env, the B4
  credential volume and mount, a restricted-PSA `securityContext`, and resource caps. The
  publisher adds only that fixed credential volume. It never accepts user-supplied `sidecars`,
  `mounts`, volumes, or podOverrides, and the B6 admission policy enforces the same allowlist.
  Labels hold bounded values only: an owner identifier and an access level (always `keycloak` in
  pass 1, so pass 2 can add `public` without relabeling). The unverified git remote and commit
  go in annotations (A7). The token Secret carries an ownerReference to its MarimoNotebook, so
  deleting the CR garbage-collects it.
- Rollout is asynchronous. The publisher waits for a Pod whose F8 content-hash env matches the
  request, verifies its args, volumes, and ServiceAccount (F7), and waits for it to be Ready.
  Only then does it create or update the ApisixRoute. On timeout or mismatch it deletes what it
  created for a new app, or restores the previous CR for an update, and returns an error.
- State lives only in the CRs (D6).

**B6. Publisher infrastructure.** In `marimo_data`:

- the publisher Deployment and ServiceAccount;
- a Role in `marimo` scoped to `marimonotebooks` and `apisixroutes`, plus read-only `pods`,
  `pods/log`, and `events`. On `secrets` it gets only `create`, `patch`, and `delete`, never
  `get`, `list`, or `watch`. The publisher writes each app token once, and APISIX reads it through
  the plugin `secretRef`, so the publisher can't read any Secret in `marimo`, including the B4
  warehouse credential. Tokens are per app, not shared: every author can read their own app's
  token file, and a shared token would let one author reach every other app's Service directly;
- a `ValidatingAdmissionPolicy` (GA since Kubernetes 1.30; the data clusters run 1.36) on
  `marimonotebooks` in `marimo`. It rejects any `sidecars` or `mounts`, any podOverrides volume
  other than the app's own token Secret and the B4 credential Secret, and any ServiceAccount
  other than `marimo-published`. That closes the indirect path where a compromised publisher
  renders a pod that mounts another app's Secret;
- the `hub.services` entry and its API token. Group sync for the approver role (D10) comes
  with pass 2;
- Pod Security Admission `restricted` enforced on `marimo`. With the publisher never rendering
  sidecars, write access to `marimonotebooks` can't become a privileged pod;
- a `ResourceQuota` and `LimitRange` on `marimo`;
- the image tag through `versions.py` and Renovate;
- Concourse wiring through the existing `marimo-data` `simple_pulumi` entry. `jupyterhub_data`
  and `marimo_data` deploy through separate pipelines, so the order is explicit.
  `jupyterhub_data` applies first, registering the hub service and writing its API token to
  Vault. `marimo_data` applies second, and the publisher reads that token through VSO. Without
  the token the publisher fails closed, because it can't validate any caller.

**B7. Front ends.** An `ol-notebook publish <file> --name <n>` CLI in the image (pass 2 adds
`--access public`), plus a JupyterLab "Publish notebook…" context-menu and command-palette entry. Both call the
B5 API and show the resulting URL and status.

**B9. Observability and lifecycle.** Alert on published pods stuck in Pending or Failed, or
restarting, from kube-state-metrics pod phase and restart metrics. Add an owner-facing status view in
the publisher. Stale-app policy: see Q4.

**B10. Docs.** Update `platform-engineering-site/docs/application_specific_guides/jupyterhub/data_platform_notebooks.md`
with the edit, version, and publish flow, and update the template README. The approve flow is
added in pass 2.

### Pass 2: public apps (deferred, D11)

None of this ships in the first pass. It is kept here so pass 1 leaves room for it.

**B8. Approval to go public.** A `--access public` request publishes as gated with
`requested-access=public` and posts to Slack. An approver, a member of the approver group synced
from a Keycloak role in the `ol-data-platform` realm (D10; name: see Q3), reviews the notebook
source and data level in the publisher UI, then approves or rejects. Decisions are recorded as
CR annotations and Kubernetes Events.

Requested and effective access are separate fields. An approval names the content hash it
reviewed, and applies only while the CR still carries that hash, so an `update` that races an
approval invalidates it. Order matters in both directions:

- Going public: swap the credential to `notebook_public`, wait for the Pod with the approved
  hash and the new credential to be Ready and verified, and only then remove the OIDC plugin
  from the route.
- `update` to a public app: first restore the OIDC plugin and wait for APISIX to reconcile it,
  then change content or credentials. The app then waits for re-approval.

Pass 2 also needs:

- a native `notebook_public` StarRocks role (grants: see Q1), a Vault role for it (F14), and a
  second `VaultDynamicSecret`;
- a reproducible dependency set for public apps. `marimo run --sandbox` resolves dependencies
  again on every pod start (F10), and `==` pins on direct PEP 723 dependencies leave transitive
  and URL/VCS dependencies free to change after approval. Pass 2 needs a complete, hash-verified
  lock that the sandbox consumes, or an immutable image built per approved revision. Which of the
  two is decided in pass 2;
- `limit-count` on public routes;
- review endpoints on the publisher, restricted to the approver group (D10):
  - list pending requests;
  - fetch the exact source and data level for a content hash;
  - approve or reject a content hash. Repeating a decision on the same hash is a no-op, and a
    decision on a stale hash is refused.

  That needs hub service scopes to read users and groups, `manage_groups=True` and
  `auth_state_groups_key` in `jupyterhub_data`, and a Keycloak mapper that puts the role in
  the token.

## Sequencing

A4 and A6 are independent and can ship first. A1 comes before A2, A3, and A5, and B3 builds on
A2 and A3. B1 gates B5. B2 does not, once B1 has proven the stopgap. B5 needs B3 for its image
and can't deploy without B6 (its ServiceAccount, Role, and hub service entry), so B5 and B6
land together, followed by B7. B4 needs StarRocks E7 for connectivity. Pass 2 (B8) starts after pass 1 is in production, and needs
answers to Q1 and the approver half of Q3. The first real consumer is the
feedback-clustering curation notebook (`tk-mvp-consumption-surfaces-superset-cluster-triage-ba37c2`).

## Open questions

- **Q1.** What does `notebook_public` grant? A dedicated reporting database or a named list of
  marts is simplest to audit. This blocks pass 2 only.
- **Q2.** Should notebook repos be per user, or one shared `mitodl/ol-data-notebooks` with a
  directory per user? I recommend the shared repo: provenance (and, in pass 2, public-app
  review) then happen in one place, and CODEOWNERS per directory keeps ownership clear. The cost is that every
  notebook author needs write access to it.
- **Q3.** What is the approver role called (pass 2)? One Keycloak role in `ol-data-platform`,
  e.g. `notebook_publish_approver`.
- **Q4.** What is the stale-app policy? For example, unpublish apps whose owner has left MIT, and
  flag apps with no requests in 90 days.
- **Q5.** Who may publish a gated app? Resolved as D12: anyone with hub access.

## Verification

- Part A: time-to-first-render of `getting_started.py` on a fresh and a restarted pod, before and
  after, from the A1 harness. A double-click on a `.py` opens marimo. A new user lands on the
  getting-started notebook.
- Part B pass 1, in CI then QA: publish through the CLI, and confirm the gated URL requires a
  Keycloak login, and that the app sees no viewer tokens or cookies (D9). Confirm that a request
  without a hub token is refused (D12), and that an in-cluster request without the marimo token
  is refused (D7). Confirm that admission rejects a MarimoNotebook carrying an extra volume or
  a sidecar (B6), and that another user's `update` or `unpublish` of the app is refused (B5).
  Update the content, and confirm the new version serves (F8).
  Unpublish, and confirm the CR, route, ConfigMap, Secret, and Pod are gone.
- Part B pass 2: request public, approve it as an approver, and load the URL unauthenticated.
  Confirm in the StarRocks audit log that queries run as `notebook_public`.
