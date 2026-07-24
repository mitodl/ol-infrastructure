# Concourse Dashboard Reorganization — Spec

Status: spec (approved for phased implementation)
Target: `https://cicd.odl.mit.edu` (odl-prod), Concourse 8.2.4
Tracking: `wp-concourse-pipeline-dashboard-reorganization-a35616`, epic
`tk-reorganize-concourse-pipeline-dashboard-for-clar-aa08bf`

## 1. Problem

The odl-prod Concourse dashboard is unusable as a navigation surface. Pipelines
are laid out in creation order within each team, with no grouping, so finding a
specific pipeline requires the search box and prior knowledge of its name.

Live inventory (captured 2026-07-25 via `fly -t odl-prod pipelines --all --json`):

| Team | Pipeline objects | Distinct names (= dashboard tiles) |
| --- | ---: | ---: |
| `infrastructure` | 113 | 84 |
| `main` | 35 | 32 |
| `ocw` | 11704 | 10 |
| `xpro` | 4 | 4 |
| `mitx` | 3 | 3 |
| `mitxonline` | 3 | 3 |
| `mitx-staging` | 2 | 2 |
| `resources` | 0 | 0 |

The object/tile gap is instanced pipelines, which Concourse already collapses
into one tile per instance group:

- `infrastructure/jupyter_notebook_docker_image_build` — 30 instances keyed on
  `image_name` (`uai_source-uai.*`, per-course authoring images).
- `main/google-ads-optimization` — 4 instances keyed on `course_name`, all paused.
- `ocw` — 2932 `draft`, 2931 `live`, 2914 `draft-ocw-course-v3`, 2914
  `live-ocw-course-v3`, plus 8 `mass-build-sites` instances and 5 singletons.

**`ocw` is therefore not a problem and is out of scope.** Its 11704 pipelines are
already the intended use of instance groups and render as 10 tiles. The real
scope is `infrastructure` (84 tiles) and `main` (32 tiles).

## 2. Hard constraints

These were established during discovery and bound every option below.

### 2.1 Worker pools are bound to teams, not step tags

`fly workers --json` reports `"team": "infrastructure"` on the privileged EC2
workers. That binding originates in
`src/ol_infrastructure/applications/concourse/Pulumi.{CI,QA,Production}.yaml`
(`concourse_team:` per `worker_def`), flows through `build_worker_user_data()` in
`src/ol_infrastructure/applications/concourse/__main__.py` into
`/etc/default/concourse-team` on the worker AMI, and is passed to the worker as
`--team`. A worker registered with a team is exclusive to it.

Only `infrastructure` and `ocw` have dedicated pools. Every other team shares the
unbound global pool. `src/ol_concourse/lib/jobs/infrastructure.py` (the
`pulumi_job` / `packer_jobs` factories) uses no step-level `tags:` anywhere.

A worker takes **exactly one** team, or none. There is no multi-team worker:
concourse/concourse#2633 ("Concourse workers can be associated with multiple
teams") was closed **"not planned"**. Confirmed against the 8.2.4 worker docs —
"you can configure a worker to belong to a single team".

Live shape (`fly -t odl-prod workers --json`, 2026-07-24): 18 workers — 7 bound
to `infrastructure`, 7 to `ocw`, 4 global. **No worker carries any tag today.**

**Revised 2026-07-24 (supersedes the original "out of scope" ruling).** Splitting
`infrastructure` into semantic teams is now the chosen direction (§Phase 3), on
the decision that team scoping beats naming prefixes. Since a pool cannot serve
several teams, that reduces to exactly two implementations:

| | Mechanism | Boundary | Cost |
| --- | --- | --- | --- |
| **A (chosen)** | Drop `concourse_team` from the infra pool, tag it, route jobs with step-level `tags:` | IAM boundary becomes a **convention** | none |
| B | One dedicated ASG per semantic team | Boundary stays **enforced** | min 2 → 8 `m7a.4xlarge`, on-demand floor 1 → 4, burst capacity fragmented |

Option B was rejected on cost and because fragmenting pools partially undoes the
spot diversification shipped in #5112 — small pools are exactly what made CI
fragile during the t3a exhaustion.

**What Option A actually costs, stated plainly.** The infra pool's instance
profile carries `base`, `infra`, `operations`, `cloud_custodian`, `pulumi_state`.
Any container on those hosts can reach IMDS and assume that role. Today
`concourse_team: infrastructure` *is* the authorization boundary. Once the pool
is global, Concourse tags are a **scheduling hint, not authorization** — anyone
who can set a pipeline in any team can add `tags: [pulumi]` and get that role.
Tagging does prevent *accidental* scheduling (untagged builds will not land on a
tagged worker), so the exposure is deliberate misuse, not drift. Accepting this
implies the set of people who can set pipelines in *any* team is already the set
trusted with the Pulumi deploy role — which is precisely what Phase 5 (teams as
code) would make reviewable, and is therefore now a prerequisite rather than an
independent workstream.

Implementation is smaller than expected: `build_worker_user_data()` in
`src/ol_infrastructure/applications/concourse/__main__.py` **already** supports
`concourse_tags` → `/etc/default/concourse-tags` → `CONCOURSE_TAG`. The missing
half is a `tags:` parameter on `pulumi_job` / `packer_jobs`, which live in the
separately published `mitodl/ol-concourse` package — a cross-repo change plus a
version bump here.

### 2.2 Renames have three-way coupling

Any rename or team move must be checked against:

1. **`release_bot` hardcodes names and team.**
   `src/ol_infrastructure/applications/release_bot/__main__.py` and
   `concourse_client.py` pin `CONCOURSE_TEAM=infrastructure` plus nine exact
   pipeline names: `learn-ai-pipeline`, `mit-learn-pipeline`,
   `mit-learn-nextjs-pipeline`, `mitxonline-pipeline`, `xpro-pipeline`,
   `ocw-studio-pipeline`, `odl-video-service-pipeline`, `micromasters-pipeline`,
   `ol-analytics-api-pipeline`. Renaming any of these breaks the Slack slash
   commands until the bot config changes in lockstep.
2. **Pipelines are self-managing via meta registries.** Every pipeline is
   re-applied by a meta pipeline (`PIPELINE_CONFIGS` in
   `src/ol_concourse/pipelines/infrastructure/meta.py` and
   `container_images/meta.py`; `production_app_names` in
   `simple_pulumi/meta.py`; `app_names` in `k8s_apps/meta.py`; the six
   `open_edx/*/meta.py` files; `libraries/configuration.py`). A live-only
   `fly rename-pipeline` is silently reverted on the next meta run. A source-only
   registry edit creates a new pipeline and orphans the old build history. A real
   rename is `registry edit + fly rename-pipeline` landed together, ahead of the
   meta pipeline's next git-triggered run.
3. **Vault credential paths are team-scoped — but by *group*, not per pipeline.**
   Deployed config (`src/bilder/images/concourse/deploy.py:186`) is
   `vault_path_prefix="/secret-concourse"`, `vault_shared_path="shared"`, so the
   lookup chain is `/secret-concourse/{team}/{pipeline}/{secret}` →
   `/secret-concourse/{team}/{secret}` → `/secret-concourse/shared/{secret}`.

   In practice **nothing uses the pipeline-scoped tier.** References are
   `((group.key))` — a KV entry per group, fields within it — resolving via the
   `{team}/{secret}` template. So a team move relocates *groups*, not per-pipeline
   secrets, which makes the migration far smaller than "83 pipelines × secrets".

   Authoritative inventory, `vault kv list secret-concourse/<team>` against
   vault-production (KV v2), 2026-07-24:

   | Team | Groups |
   | --- | --- |
   | `infrastructure` | cortextool, dbt, dockerhub, eks, fastly, github, github_enterprise, grizzly, mit-github, open_api_clients, superset_qa_service_account, superset_service_account, unified_ecommerce_api_clients (13) |
   | `main` | consul, dockerhub, fastly, google_ads_optimization, grafana, npm_publish, platform_engineering_site, pypi_creds (8) |
   | `shared` | fastly, sentry (2) |
   | `ocw` | api-bearer-token, draft/, fastly_draft, fastly_live, fastly_test, git-private-key, healthchecks-io-{draft,live}-uuid, live/, slack-url |
   | `mitx` / `mitx-staging` | github |
   | `mitxonline` / `xpro` | github, tubular_oauth_client |

   **Do not derive this from the SOPS file.** `src/bridge/secrets/concourse/
   operations.production.yaml` is missing five live groups (§4.5).

   Mapping `infrastructure`'s 13 groups onto the §3 clusters, by which pipeline
   files reference them:

   - **core-platform** — `cortextool`, `grizzly` (both `grafana_cloud/pipeline.py`)
   - **data-platform** — `dbt` (`dagster/`), `superset_service_account` +
     `superset_qa_service_account` (`ol_superset_deploy.py`)
   - **open-edx** — `github_enterprise`, `open_api_clients` (both
     `grader_images/build_pipeline.py`)
   - **promote to `shared`** — `dockerhub` (17 referencing files spanning every
     cluster) and `github` (`k8s_apps/`, `container_images/jupyter_courses.py`)
   - **verify before moving** — `eks`, `fastly` (a `shared` copy already exists),
     `mit-github`, `unified_ecommerce_api_clients`. The last two have **zero**
     `((...))` references anywhere under `src/ol_concourse` and may be dead.

   So the migration is: 2 groups promoted to `shared`, 5 moved with their
   cluster, 4 to triage. That is tractable, and it is the strongest argument that
   the team split is affordable.

### 2.3 Concourse has no folder/tag primitive

Confirmed against concourse-ci.org docs for 8.2.4. The complete set of
cross-pipeline organization primitives is:

| Primitive | What it does | Reversible |
| --- | --- | --- |
| Teams | Hard partition + RBAC + worker binding | Yes, but see 2.1/2.2 |
| Instance groups | One tile for templated variants of *the same* pipeline | Yes |
| `fly order-pipelines` | Cosmetic display order within a team | Yes |
| `fly archive-pipeline` | Hides tile, preserves config + history | Yes (`unpause`/re-set) |
| `fly rename-pipeline` | Renames, preserves build history | Yes |
| `display.background_image` | Per-pipeline tile background image | Yes |

There is no native way to nest or label distinct pipelines inside a team. Visual
grouping must be emulated by **ordering + naming**.

### 2.4 Version: already current; no newer grouping feature to wait for

`CONCOURSE_VERSION = "8.2.4"` in `src/bridge/lib/versions.py`. Concourse v8.0.0
shipped January 2026, so we are already on the current major line — there is no
upgrade that would unlock a grouping primitive. The upstream request for pipeline
folders/grouping (concourse discussion #8827) is open with no implementation. Do
not gate the dashboard work on an upgrade.

The one upstream item with an implementation behind it is discussion #9660 →
**PR concourse/concourse#9661**, which adds a pipeline-level `description` field.
That is a pipeline-page feature, not a dashboard one — see Phase 6. It is the
only reason a future version bump matters to this project.

(Side note: the pin's inline comment reads "Pin to <8.1.0 because of some login
bugs with stale state tokens" while the pinned value is 8.2.4. The comment is
stale and contradicts the value; worth correcting separately.)

### 2.5 The dashboard search bar is URL-addressable — this is a real grouping lever

Not previously accounted for. The Concourse dashboard's search field supports
selectors (`team:<name>`, `status:succeeded|failed|running|pending|paused|
errored|aborted`, plus fuzzy name match) **and reflects them into query
parameters**, so a filtered view is a shareable, bookmarkable URL:

- `https://cicd.odl.mit.edu/?search=foo` — all pipelines matching `foo`
- `/?team=main&fuzzyName=pie` — equivalent of `team: main pie`
- `/?status=running&status=pending`

This does not replace ordering — it does not change what the default landing page
looks like, and it is opt-in per person. But it means a curated set of "virtual
folder" links (README, team wiki, Slack bookmarks) is achievable today with no
server-side change at all, e.g. one link per §3 cluster.

It also materially strengthens the case for **Phase 3 naming prefixes**: once
names are `data-*`, `core-*`, `edx-*`, a bookmark of `/?search=data-` becomes a
genuine, self-maintaining virtual folder rather than a hand-curated list of names
that rots. Phase 3's payoff is therefore larger than "alphabetical order looks
nicer".

Verify the exact parameter names against the running 8.2.4 UI before publishing
any bookmark set — the mapping above is from upstream issues
(concourse/concourse#1684, #1703, #3573), not from the 8.2.4 docs.

## 3. Approach

Ordering + naming, applied in risk-ascending phases. Phase 1 is pure display
state and delivers most of the value; nothing after it is required for the
dashboard to be usable.

### Phase 1 — Curated ordering (`fly order-pipelines`) — zero risk

`fly order-pipelines --team <team> -p <name> -p <name> ...` sets a display-order
field via the API. It touches no pipeline config, no build history, no
credentials, and no source. Re-running it with a different list fully reverts.

Both orderings below were verified for exact coverage against the live inventory
— 84/84 for `infrastructure`, 32/32 for `main`, no duplicates, no phantoms.

#### `infrastructure` (84 tiles, 6 clusters)

1. **meta** — the registries that generate everything else, so the dashboard
   starts with "where do pipelines come from":
   `pulumi-infrastructure-meta`, `simple-pulumi-meta`, `k8s-apps-meta`,
   `dagger-pulumi-edxapp-meta-v3`, `packer-pulumi-xqwatcher-meta`,
   `xqwatcher-grader-images-meta`
2. **core-platform** (25) — AWS substrate, clusters, AMIs, identity,
   observability, notification:
   `pulumi-aws`, `pulumi-aws-ecr`, `pulumi-aws-sftp`, `pulumi-eks-cluster`,
   `packer-docker-baseline`, `packer-pulumi-vault`, `packer-pulumi-consul`,
   `packer-pulumi-concourse`, `docker-pulumi-keycloak`, `pulumi-monitoring`,
   `misc-grafana-management`, `pulumi-vector-log-proxy`, `pulumi-kubewatch`,
   `pulumi-sentry`, `pulumi-rootly`, `misc-cloud-custodian`, `pulumi-mailgun`,
   `pulumi-fastly-redirector`, `pulumi-xpro-partner-dns`,
   `pulumi-celery-monitoring`, `pulumi-release-bot`, `pulumi-toolhive-operator`,
   `pulumi-toolhive-data`, `pulumi-toolhive-apps`, `pulumi-toolhive-swe`
3. **data-platform** (20) — warehouse, ingestion, query engines, catalog,
   notebooks:
   `pulumi-data_warehouse`, `pulumi-airbyte`, `docker-pulumi-dagster`,
   `pulumi-clickhouse`, `pulumi-starrocks`, `pulumi-starrocks-substructure`,
   `pulumi-starburst`, `pulumi-open-metadata`,
   `pulumi-open-metadata-substructure`, `pulumi-opensearch`,
   `pulumi-qdrant-cloud`, `pulumi-mongodb-atlas`, `pulumi-tika`,
   `docker-pulumi-superset`, `ol-superset-deploy`, `ol-analytics-api-pipeline`,
   `pulumi-jupyterhub`, `pulumi-jupyterhub-data`, `pulumi-marimo-data`,
   `jupyter_notebook_docker_image_build`
4. **open-edx** (16) — grouped by component, then by deployment
   (master/ulmo/verawood):
   `dagger-pulumi-edxapp-global`, `dagger-pulumi-codejail-{master,ulmo,verawood}`,
   `dagger-pulumi-edx-notes-{master,ulmo,verawood}`,
   `docker-packer-pulumi-xqueue-{master,ulmo,verawood}`,
   `docker-pulumi-xqwatcher-{master,ulmo,verawood}`, `build-grader-base-image`,
   `build-graders-mit-600x-image`, `build-graders-mit-686x-image`
5. **applications** (12) — product deploy pipelines, `release_bot`-referenced ones
   first:
   `mit-learn-pipeline`, `mit-learn-nextjs-pipeline`, `learn-ai-pipeline`,
   `mitxonline-pipeline`, `xpro-pipeline`, `micromasters-pipeline`,
   `ocw-studio-pipeline`, `odl-video-service-pipeline`, `pulumi-ocw-site`,
   `pulumi-open-discussions`, `pulumi-digital-credentials`,
   `pulumi-b2b-partners-storage`
6. **deprecated** (5) — parked last pending the Phase 2 archive decision (§4):
   `docker-pulumi-micromasters-relesae-test`, `docker-pulumi-ovs-relesae-test`,
   `docker-pulumi-xpro-relesae-test`, `dcind-resource-image`,
   `docker-google-ads-opt-image`

Placement decisions worth recording: **Keycloak sits in core-platform, not
open-edx** (confirmed with the user — it is the org-wide IdP, not an Open edX
component). `ol-analytics-api-pipeline` sits in data-platform rather than
applications despite being a `k8s_apps` app, because it is read as part of the
data stack; it is still `release_bot`-referenced and must not be renamed.

#### `main` (32 tiles, 6 clusters)

1. **meta** (8) — `container-images-meta`, `ol-concourse-resource-meta`,
   `ol-api-clients-meta`, `publish-python-packages-meta`, `mfe-meta-pipeline`,
   `dagger-pulumi-codejail-meta`, `dagger-pulumi-edx-notes-meta`,
   `docker-packer-pulumi-xqueue-meta`
2. **concourse-tooling** (8) — resource types and CI images:
   `build-ol-concourse-images`, `publish-ol-concourse`,
   `build-concourse-resources-rclone`, `build-concourse-resources-s3-sync`,
   `docker-concourse-vault-resource`,
   `docker-hashicorp-release-resource-image`,
   `docker-mitodl-concourse-npm-resource`, `dcind-resource-image`
3. **base-images** (7) — shared build/runtime images:
   `ol-python-base-docker`, `ol-infrastructure-docker-container`,
   `docker-openedx-tubular-image`, `ocw-course-publisher-image`,
   `ol-superset-image`, `build-redash-image`, `docker-google-ads-opt-image`
4. **package-publishing** (4) — `publish-open-edx-plugins-pypi`, `publish-jupyterhub-extensions-pypi`,
   `mit-learn-api-client`, `mitxonline-api-client`
5. **apps-misc** (4) — `platform-engineering-site`, `edx-sysadmin`,
   `sign-and-verify`, `google-ads-optimization`
6. **deprecated** (1) — `publish-ol-django-pypi`, parked pending archive (§4.4)

The canonical ordering lists live in `bin/` (see §6) so they can be re-applied
idempotently rather than being one-shot shell history.

**Verification item — resolved 2026-07-24 on `odl-ci`, outcome as expected.**
`set_pipeline` does **not** reset display order, so Phase 1 is a run-once
deliverable rather than a scheduled one.

Method: created three throwaway paused pipelines on `odl-ci`'s `infrastructure`
team, reordered them to the top via `fly order-pipelines`, then re-set one of
them with a genuinely *changed* config (confirmed written by diffing
`fly get-pipeline` — `v2-changed` on the target, `v1` on an untouched sibling).
The display order was byte-identical before and after. Newly-created pipelines
were observed appending to the end of the team, never disturbing existing
positions. Test pipelines were destroyed and the team's original order restored.

Consequence: ordering only needs re-applying when the pipeline *set* changes, not
on every meta run. `check` is what detects that — it exits non-zero when a live
pipeline is missing from the curation (it lands in `unclustered`) or a curated
name no longer exists.

### Phase 2 — Archive genuinely dead tiles

See §4 — this is now a *correctness* finding, not just cosmetics.

### Phase 3 — Semantic teams on a globally-tagged worker pool

**Direction changed 2026-07-24.** Team scoping replaces naming prefixes as the
primary grouping mechanism. Teams give real partitioning — separate dashboards,
separate RBAC, separate secret namespaces — where prefixes only simulate grouping
through sort order. Prefixes remain available as a *complement* within a team
(and the rename mechanics below still apply if we take them up), but they are no
longer the plan.

Target teams mirror the §3 clusters: `core-platform`, `data-platform`,
`open-edx`, `applications`. `meta` and `deprecated` are dispositions rather than
domains — meta pipelines follow whatever they generate, and the deprecated five
are resolved by Phase 2 instead of being moved.

Sequence, in dependency order:

1. **Phase 5 first, not in parallel.** Teams-as-code becomes a prerequisite:
   Option A (§2.1) makes "who can set a pipeline in any team" equivalent to "who
   can assume the Pulumi deploy role", so team membership must be reviewable
   before the boundary is relaxed, not after.
2. **Tag the pool, keep it bound.** Add `concourse_tags` to the infra `worker_def`
   while `concourse_team: infrastructure` is still set, and confirm existing jobs
   still schedule. Tagged workers only accept tag-requesting builds, so this step
   is where that gets proven without giving anything up.
3. **Add `tags:` to `pulumi_job` / `packer_jobs`** in `mitodl/ol-concourse`,
   release, bump here, regenerate. Confirm every infra pipeline still lands on the
   infra pool.
4. **Drop `concourse_team`** from the infra `worker_def`. This is the step that
   trades the enforced boundary for a conventional one — land it on its own.
5. **Move secrets before pipelines.** Promote `dockerhub` and `github` to
   `shared`; copy each cluster's groups to its new team path (§2.2.3). Copy, don't
   move — leave the `infrastructure` originals until the move is proven, then
   clean up.
6. **Move pipelines per cluster, smallest first.** `open-edx` (2 secret groups) is
   the best pilot. Per cluster: `fly set-team`, update the meta registry's
   `SetPipelineStep` team, `fly rename-pipeline`/re-set as needed, re-run
   `bin/concourse-order-pipelines check`.
7. **`release_bot` last.** It pins `CONCOURSE_TEAM=infrastructure` plus nine
   pipeline names (§2.2.1). The `applications` cluster holds eight of the nine, so
   that cluster cannot move until the bot takes a team-per-pipeline mapping.

Risks specific to this path, beyond §2.1's IAM trade: `ol-analytics-api-pipeline`
is `release_bot`-referenced but sits in **data-platform**, so the bot's mapping
must be per-pipeline, not per-cluster. And the `infrastructure` team keeps its
name and its worker pool throughout — only pipelines leave it — so nothing has to
be re-registered at any point.

#### Deferred: semantic naming prefixes

Retained for reference. Current naming encodes the *tool* (`pulumi-`, `packer-`,
`docker-`, `dagger-pulumi-`) rather than the *domain*. If taken up later, the
target shape is `<domain>-<component>` and the per-pipeline sequencing rule is:

1. Verify the name is not in the `release_bot` nine (or update the bot first, in
   its own deploy).
2. Migrate/duplicate the Vault KV secrets to the new
   `/concourse/infrastructure/<new-name>/` path.
3. Land the registry edit and run `fly rename-pipeline -o <old> -n <new>` in the
   same change window, before the meta pipeline's next git-triggered run.
4. Re-apply the Phase 1 ordering (names changed).

Recommendation: defer Phase 3 until Phases 1–2 have been live long enough to
judge whether ordering alone was sufficient. Ordering delivers most of the
navigational benefit at none of the risk.

### Phase 4 — `main` grab-bag split (evaluate, don't assume)

`main` is not worker-bound, so pipelines can move teams freely from a scheduling
standpoint. But moving a pipeline out of `main` breaks its Vault path (§2.3) and
`main` has a special property: `CONCOURSE_MAIN_TEAM_GITHUB_TEAM` defaults to
`mitodl:odl-engineering` and is the only team whose RBAC is currently
declaratively managed (via the bilder/Pulumi AMI config). Everything else is
manual.

The empty `resources` team is a natural destination for the
concourse-tooling + base-images clusters (15 tiles). Evaluate against: Vault
secret migration cost, whether anyone's `fly` targets/CI scripts assume `main`,
and whether the split actually beats Phase 1 ordering. **Do not execute without
that evaluation.**

### Phase 5 — Teams as code

There is no "teams as code" for Concourse in this repo today; team creation and
RBAC are manual `fly set-team` invocations, unlike everything else here.
`fly set-team -n <team> -c <file>.yml` accepts a YAML config with a `roles:` list
mapping GitHub org/teams to Concourse roles (owner/member/pipeline-operator/
viewer). Proposal: a `src/ol_concourse/teams/<team>.yml` directory plus a
pipeline job that applies each file, making the eight teams' membership
reviewable. Auth backend is already GitHub OAuth, so `roles:` entries map onto
existing `mitodl:*` GitHub teams.

**Reclassified 2026-07-24: this is now a prerequisite for Phase 3, not an
independent workstream.** Option A (§2.1) makes pipeline-set access in any team
equivalent to holding the Pulumi deploy role, so team membership has to be
reviewable before that boundary is relaxed.

### Phase 6 — Per-pipeline descriptions: wait for upstream, don't ship the SVG workaround

**De-scoped.** The background-image SVG approach is dropped in favor of a proper
upstream field.

Concourse 8.2.4 has no native pipeline description field, and
`display.background_image` was the only pipeline-level visual hook. A working
SVG-caption workaround was built and validated on the unmerged branch
`worktree-concourse-pipeline-descriptions`
(`src/ol_concourse/pipelines/description_svg.py`, plus an example SVG and demo
wiring in `examples/hello.py`). It works, but it is a workaround: the background
layer is an absolutely-positioned `background-size: cover` div that competes with
the job-graph SVG layer with no coordination between them, cannot be panned, and
crops unpredictably depending on the viewer's viewport aspect ratio.

That limitation is what motivated fixing it upstream instead:

- Discussion: https://github.com/orgs/concourse/discussions/9660
- **PR: https://github.com/concourse/concourse/pull/9661** — "Add pipeline-level
  description field" (open, authored by blarghmatey, +196/-4 across 15 files)

The PR adds a top-level `description` string to `Config`, plumbed through DB
storage and the API the same way `Display` already is, rendered as markdown via
`dillonkearns/elm-markdown`'s unmodified `defaultHtmlRenderer` (so embedded raw
HTML does not render — the description is visible to anyone with view access,
including anonymous viewers of public pipelines). Critically it renders as a flow
*sibling* of the groups bar rather than a child of `.pipeline-content`, so it
reserves its own space and pushes the graph down instead of fighting it for the
same absolute layer.

**Important scope caveat for this project:** PR #9661 renders the description on
the **pipeline page only**. Dashboard tiles use a separate `Pipeline` type that
does not carry `description`, deliberately left out of that first pass. So this
field does **not** declutter the dashboard — Phases 1 (ordering) and 2.5 (filter
URLs) remain the only dashboard levers. It solves per-pipeline *documentation*,
which is what the SVG task was actually reaching for.

Adoption is blocked on a chain we do not control: PR #9661 merges → lands in a
Concourse release → we bump `CONCOURSE_VERSION` in `src/bridge/lib/versions.py`
→ `description` is added to `Pipeline` in `ol_concourse.lib.models.pipeline`
(the separate `mitodl/ol-concourse` package, published by the `publish-ol-concourse`
pipeline) → descriptions authored per pipeline in the meta registries.

Until that chain completes there is nothing to build here. Do not ship the SVG
workaround in the meantime, and do not resurrect PR #5097 (closed unmerged) or
the throwaway `misc-cloud-hello` pipeline (destroyed off odl-prod). Keep
`worktree-concourse-pipeline-descriptions` unmerged as a record; if #9661 is
rejected upstream, that branch is the fallback to reconsider.

## 4. Findings that outgrew "dashboard cleanup"

Investigating the archive candidates turned up two live correctness problems.
These are the most important output of this phase.

### 4.1 The `-relesae-test` pipelines are live shadow deploy pipelines

`docker-pulumi-micromasters-relesae-test`, `docker-pulumi-ovs-relesae-test`, and
`docker-pulumi-xpro-relesae-test` have **no definition anywhere in this repo**
(`rg -n 'relesae' src/` → nothing). They were hand-set via `fly` and never
codified. They are not dormant:

- `docker-pulumi-micromasters-relesae-test` — `build-micromasters-image-from-master`
  build #16 succeeded 2026-07-22.
- `docker-pulumi-ovs-relesae-test` —
  `deploy-ol-application-odl-video-service-qa` build #54 **failed** 2026-07-24;
  #53 failed 2026-07-23. Failing repeatedly.
- `docker-pulumi-xpro-relesae-test` — `build-mitxpro-release-image` #12 failed
  2026-06-15.

Each is a full legacy pre-Kubernetes clone of a current `k8s_apps` pipeline —
same app, `pulumi-provisioner` resources, github-deployments, release-gate
issues, Slack alerts, and **`deploy-ol-application-<app>-production` jobs**. They
duplicate `micromasters-pipeline`, `odl-video-service-pipeline`, and
`xpro-pipeline` respectively.

Risk: an unmanaged, unreviewed pipeline holding a production deploy job for three
live products, with no source of truth. Archiving is the right outcome but is
**not** the zero-risk cleanup it was assumed to be — confirm with the product
owners that the EC2/`ol-application` deploy path is genuinely retired for each
app before archiving. `fly archive-pipeline` is reversible, so archive-first is
acceptable if a quick rollback path is agreed.

### 4.2 Two container-image pipelines are duplicated across teams and double-building

`dcind-resource-image` and `docker-google-ads-opt-image` exist in **both**
`infrastructure` and `main`. `container-images-meta` lives in `main` and its
`SetPipelineStep`s carry no `team=`, so `main` holds the canonical, registry-
managed copies (both last configured 2026-06-18). The `infrastructure` copies are
orphans — last config update 2026-03-25 and 2026-06-10, before the `main` copies
existed — that nothing in the repo maintains.

They are still running: `docker-google-ads-opt-image` built at
`2026-06-26@06:28:32` in *both* teams simultaneously, burning ~7min of worker
time on `infrastructure` and ~15min on `main` to publish the same image.
`infrastructure/dcind-resource-image` reached build #56 on 2026-06-18 while
`main/dcind-resource-image` build #1 is still `pending` — i.e. the *main* copy has
never actually completed a build, so archiving the `infrastructure` copy needs the
`main` copy unstuck first.

Action: unstick `main/dcind-resource-image` build #1, confirm both `main` copies
publish successfully, then archive the two `infrastructure` copies. Until then,
they sit in the **deprecated** cluster at the bottom of the ordering.

### 4.4 `publish-ol-django-pypi` has been failing for six weeks

Found by the §2.2.3 secret inventory — and it is exactly the failure mode a team
move causes, already present in `main`.

Every job errors in 1–2s since 2026-06-08. `fly watch` on
`publish-ol-django-pypi/build-common` #29:

```
failed to interpolate task config: undefined vars: github.odlbot_private_ssh_key
errored
```

`src/ol_concourse/pipelines/libraries/ol_django.py:28` passes
`GIT_SSH_KEY: ((github.odlbot_private_ssh_key))`, but the pipeline runs on `main`
and `main` has no `github` group — the chain falls through to `shared`, which has
only `fastly` and `sentry`. `main` holds the same key under `npm_publish`, and the
sibling `api_clients_pipeline.py` (also `main`) already uses
`((npm_publish.odlbot_private_ssh_key))`. One-line fix. No `mitol-django-*`
release has published since.

**Decision 2026-07-24: not fixing.** The owner's call is that this pipeline is
incorrectly implemented as a whole and should be removed or left broken rather
than repaired — applying the one-line secret fix would resume publishing from a
pipeline whose design is wrong. Tracked and closed won't-fix as
`tk-fix-publish-ol-django-pypi-undefined-var-github--505ae3`.

Disposition: `publish-ol-django-pypi` moves out of `main`'s `package-publishing`
cluster into a new `deprecated` cluster at the bottom of the `main` ordering, and
joins the Phase 2 archive candidates (§4.1 covers the `infrastructure` ones). A
replacement for mitol-django publishing, if wanted, is a rewrite filed
separately — not a repair of this pipeline.

### 4.5 The Concourse SOPS file is not a complete secret inventory

`src/bridge/secrets/concourse/operations.production.yaml` is how these secrets
are *populated*, but five live groups have no SOPS entry:
`infrastructure/{dockerhub,mit-github,unified_ecommerce_api_clients}` and
`main/{consul,fastly}`.

`infrastructure/dockerhub` is provably load-bearing, not stale:
`((dockerhub.username))` is referenced from 17 pipeline files, and
`docker-pulumi-keycloak/build-keycloak-docker-image` #724 and
`docker-pulumi-superset/build-superset-image` #243 both succeeded on 2026-07-23/24.

Consequence for this project: the SOPS-derived inventory showed `infrastructure`
with 10 groups when Vault has 13. Any team-split reasoning must come from
`vault kv list`, not from SOPS. Tracked as
`tk-reconcile-concourse-sops-secrets-file-against-li-d0f940`.

### 4.3 Stale `simple_pulumi/README.md`

`src/ol_concourse/pipelines/infrastructure/simple_pulumi/README.md` documents 16
managed apps. `production_app_names` in that directory's `meta.py` has 33.
Trivial doc fix, independent of everything else.

## 5. Non-goals

- Re-registering or re-provisioning the `ocw` worker pool.
- Touching `ocw` (§1 — already correctly organized via instance groups).
- Renaming any of the nine `release_bot` pipelines without a coordinated bot
  update.
- Any change to worker registration, IAM, or Vault policy.

## 6. Deliverables

| Phase | Deliverable | Risk |
| --- | --- | --- |
| 1 | `bin/concourse-order-pipelines` applying the §3 orderings idempotently — **built**, validated end-to-end on `odl-ci`; awaiting an unexpired `odl-prod` token to apply | none |
| 2 | Archive decision + `fly archive-pipeline` for confirmed-dead tiles | low, reversible |
| 3 | Semantic teams on a globally-tagged pool: tag pool → `tags:` in ol-concourse → unbind → migrate secrets → move clusters | high; trades an enforced IAM boundary for a conventional one |
| 4 | `main`-split evaluation memo (execute only if it wins) | medium |
| 5 | `src/ol_concourse/teams/*.yml` + apply job — **now a prerequisite for Phase 3**, not independent | low |
| 1b | Bookmarkable per-cluster dashboard filter URLs (§2.5), published wherever the team keeps links | none |
| 6 | Per-pipeline descriptions — de-scoped; track upstream PR concourse/concourse#9661, adopt after it ships | blocked upstream |

## 7. Verification

- Phase 1: `bin/concourse-order-pipelines check` exits zero; reload the dashboard
  per team and confirm cluster boundaries are visually legible at default zoom.
  Ordering surviving `set_pipeline` is already settled (§3) and does not need
  re-testing per rollout.
- Phase 2: confirm each archived pipeline's replacement has a green build newer
  than the archived one's last green build.
- Phase 3: per rename, confirm build history carried over, Vault-backed `((var))`
  still resolves (trigger one job), and `release_bot` slash commands still work.
