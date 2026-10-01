# pre-commit.ci → prek + autofix.ci: fleet inventory

**Issue:** [ol-infrastructure#5805](https://github.com/mitodl/ol-infrastructure/issues/5805)
**Witan project:** `wp-migrate-mitodl-repositories-from-pre-commit-ci-t-3ff60d`
**Measured:** 2026-09-28, against the default branch of every active repository
**Scope updated:** 2026-10-01. Six repositories were dropped from the migration (§6). Their
measurements stay in the JSON; every other figure on this page is still the 09-28 measurement
unless it says otherwise.
**Machine-readable companion:** [`prek-autofix-migration-inventory.json`](prek-autofix-migration-inventory.json)

This is the scope document for the migration. Every later task (the migration contract, the
reference workflow, the pilots, the per-repository PRs, the final audit) works from the wave
assignments and findings here. The JSON carries the per-repository detail this page
summarises: every hook and its `rev`, local-hook entries and dependencies, `ci:` blocks,
manifest declarations, workflow references, Renovate coverage, rulesets and measured results.

---

## 1. The short version

- **28 repositories carry a `.pre-commit-config.yaml`**, and all 28 were in scope at
  measurement. 22 remain in scope after six were dropped on 2026-10-01 (§6). Three more already
  use `prek.toml`. Two support repositories carry shared configuration. 42 active
  repositories have no hook configuration and are excluded, and two private repositories are
  withheld (§6).
- **prek runs every existing configuration unchanged.** Across the 28 configurations, prek
  0.5.3 produced no failure that pre-commit 4.6.2 did not also produce on the same tree, for
  every hook it ran. Local node/system hooks and `packer_fmt` were not run (§3.1). The plan
  to keep `.pre-commit-config.yaml` as-is holds. The pilots cover the node hooks and
  `packer_fmt`. The three local `system` hooks (lehrer's two schema builders and mit-learn's
  `check-vendor-directory`) are not in a pilot, so those repositories' own migration PRs are
  their first run under prek.
- **Only 14 of the 28 are enforced by pre-commit.ci today.** The other 14 are not in the
  pre-commit.ci installation (§5), and nothing else runs their hooks in CI (apart from three
  hooks in lehrer). Their default branches have drifted: eight of them fail
  their own hooks right now. For those repos the migration adds a gate rather than replacing
  one, and each migration PR has to make its tree clean first. Of the 22 still in scope, 13
  are in the installation and 9 are not.
- **Some skipped hooks have never run in CI anywhere.** pre-commit.ci's `ci: skip` removed
  them, and no GitHub Actions job runs them instead. `hadolint-docker` is in that set and fails
  on `ol-infrastructure` main today.
- **ol-data-platform would lose hook updates when pre-commit.ci is uninstalled.** It had 15
  pre-commit.ci autoupdate PRs since June, and its own `renovate.json` does not enable
  Renovate's `pre-commit` manager. alerting-omnibus and superset-marimo get no hook updates
  from anything today: no Renovate, and no pre-commit.ci activity. Both have since been
  dropped (§6). Everything else is already covered by the org preset.
- **No required status check names a pre-commit.ci context.** Removing the app cannot wedge a
  ruleset. The new check only becomes required if the contract decides to make it required.

## 2. Reconciliation with the 2026-09-09 survey

Two private repositories are withheld from this document (§6) and excluded from every count
below and in the JSON. The 09-09 survey's method was not recorded, so a difference that is not
explained below cannot be attributed to particular repositories.

| Measure | 09-09 | 09-28 (listed) | Why it differs |
| --- | --- | --- | --- |
| Active non-fork repositories | 76 | 75 | Not attributable |
| Default branches with `.pre-commit-config.yaml` | 28 | 28 | — |
| Repositories with a pre-commit.ci `ci:` block | 3 | 12 | The 09-09 count was wrong. Twelve repositories use `ci: skip`; smoot-design also sets `autoupdate_commit_msg` |
| `pyproject.toml` declaring `pre-commit` | 9 | 8 | Not attributable |
| pre-commit.ci installation | selected scope | 22 selected repositories | Membership read by an org owner on 2026-09-28 (§5) |
| autofix.ci installation | absent | absent | Unchanged |

## 3. Waves

| Wave | Count | Rule |
| --- | --- | --- |
| **pilot** | 3 | Named in the issue: Python/Pulumi, DBT/SQL, Node |
| **complex** | 8 | Any of: a local `node`/`system`/`script` hook needing the repository's toolchain, a `ci: skip` list, or `pre-commit` called from outside the hook config |
| **standard** | 11 | Everything else: remote hooks plus, at most, local `python`/`pygrep` hooks with their dependencies declared |
| **already-prek** | 3 | `prek.toml`, no pre-commit.ci status. Only CI pinning and optional autofix.ci adoption remain |
| **supporting** | 2 | Hold shared configuration the rollout changes |
| **excluded** | 50 | §6. Includes the six dropped on 2026-10-01 (one complex, five standard) |

### 3.1 Rollout table

*pc.ci status* means a `pre-commit.ci - pr`/`push` status appeared on at least one of the
last 15 PRs. The 13 "yes" rows are exactly the in-scope repositories in the pre-commit.ci
installation, and the "no" rows are exactly the ones outside it (§5). The six repositories
dropped on 2026-10-01 are listed in §6 instead.
*autoupdate* counts pre-commit.ci autoupdate PRs opened since 2026-06-01. *Renovate hooks*
means Renovate's `pre-commit` manager is enabled, via the org preset or repository config.
*prek run* is `prek run --all-files` on the unmodified default branch; hooks in the JSON's
`not_run_locally` field (local node/system hooks, `packer_fmt`) were skipped for it.

| Wave | Repository | Hooks | Local hooks | `ci: skip` | pc.ci status | autoupdate | Renovate hooks | Required checks | prek run |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| pilot | ol-infrastructure | 19 | sync-version-pins (python) | packer_fmt, hadolint-docker | yes | 15 | yes | ci-gate | fail: hadolint-docker |
| pilot | ol-data-platform | 16 | — | sqlfluff-fix | yes | 15 | **no** | — | pass |
| pilot | smoot-design | 14 | prettier, eslint (node) | eslint, prettier | yes | 0 | yes | — | pass |
| complex | learn-ai | 17 | prettier, eslint (node); drf-serializer-orm-check (python) | prettier, eslint | no | 0 | yes | — | pass |
| complex | lehrer | 21 | lehrer-core-boundary (pygrep); build-config-schema, build-manifest-schema (system) | packer_fmt, hadolint-docker, build-config-schema, build-manifest-schema | no | 0 | yes | fast-checks, gate | pass |
| complex | mit-learn | 19 | prettier, eslint, style-lint (node); check-vendor-directory (system); drf-serializer-orm-check (python) | prettier, eslint, style-lint, check-vendor-directory | yes | 3 | yes | ci-gate, openapi-diff | pass |
| complex | mit-learn-api-clients | 15 | eslint (node) | eslint | yes | 0 | yes | — | pass |
| complex | mitxonline-api-clients | 16 | eslint (node) | eslint | no | 0 | yes | — | fail: trailing-whitespace, end-of-file, prettier, shfmt |
| complex | mitxpro | 19 | prettier, eslint, stylelint (node); drf-serializer-orm-check (python) | prettier, eslint, stylelint | yes | 16 | yes | — | fail: actionlint |
| complex | ocw-studio | 19 | prettier, eslint, stylelint (node); drf-serializer-orm-check (python) | prettier, eslint, stylelint, shfmt-docker | yes | 3 | yes | — | pass |
| complex | platform-engineering-site | 17 | — | packer_fmt | no | 0 | yes | — | fail: trailing-whitespace, end-of-file, yamlfmt, ruff-format, mypy |
| standard | mitxonline | 16 | drf-serializer-orm-check (python) | — | yes | 0 | yes | ci-gate | fail: actionlint |
| standard | ocw-hugo-projects | 8 | — | — | yes | 1 | yes | — | pass |
| standard | ocw-hugo-themes | 2 | — | — | no | 0 | yes | — | fail: detect-secrets |
| standard | odl-video-service | 16 | drf-serializer-orm-check (python) | — | yes | 8 | yes | — | fail: actionlint |
| standard | ol-concourse | 15 | — | — | no | 0 | yes | — | fail: yamlfmt, ruff-format, ruff, mypy |
| standard | ol-django | 16 | — | — | yes | 7 | yes | — | pass |
| standard | ol-infra-health-checks | 15 | — | — | no | 0 | yes | — | fail: trailing-whitespace, ruff-format, ruff, mypy |
| standard | ol-keycloak | 9 | — | — | yes | 1 | yes | — | pass |
| standard | ol-keycloakify | 2 | — | — | no | 0 | yes | — | pass |
| standard | open-discussions | 1 | — | — | no | 0 | yes | — | fail: detect-secrets |
| standard | open-edx-plugins | 17 | — | — | yes | 13 | yes | — | pass |
| already-prek | agent-kit | 20 | — | — | no | 0 | yes | — | not run |
| already-prek | django-aqueduct | 14 | — | — | no | 0 | yes | — | not run |
| already-prek | ol-analytics-api | 14 | — | — | no | 0 | yes | test | not run |
| supporting | ol-github-workflows | — | — | — | — | — | — | — | Will host the reusable workflow |
| supporting | .github | — | — | — | — | — | — | — | Hosts the Renovate preset |

Every "fail" above also fails under pre-commit on the same tree. None is a prek
incompatibility. `superset-marimo` (since dropped, §6) did not load under either tool: it
names hook `ruff-check` at `astral-sh/ruff-pre-commit` `v0.9.0`, which predates that id.

## 4. Findings the migration contract has to answer

1. **The migration PR has to fix existing hook failures.** Twelve default branches failed
   their hooks at measurement. Eight of them failed in repositories nothing currently
   enforces. Ten of the twelve, six of them unenforced, are still in §3.1; access-forge and
   hq have since been dropped (§6).
   A new check that starts red on every PR would get bypassed, so each PR either fixes the
   drift, lets the first autofix pass fix it, or narrows the hook, with the choice recorded
   in the PR.
2. **Some skipped hooks have never run in CI.** Coverage below was checked by searching each
   repository's workflows for the same tool.

   | Repository | Skipped and not run in any workflow | Skipped but covered by another workflow |
   | --- | --- | --- |
   | ol-infrastructure | packer_fmt (9 `.pkr.hcl`, clean), hadolint-docker (**fails**) | — |
   | lehrer | packer_fmt (no files), hadolint-docker | build-config-schema, build-manifest-schema (`ci.yml` calls pre-commit directly) |
   | ol-rootly-manager | packer_fmt (no files), hadolint-docker | — |
   | platform-engineering-site | packer_fmt (no files) | — |
   | ol-data-platform | sqlfluff-fix (lint half runs in pre-commit.ci) | — |
   | mit-learn | style-lint, check-vendor-directory | prettier, eslint |
   | ocw-studio | shfmt-docker | prettier, eslint, stylelint |
   | mitxonline-api-clients, mit-learn-api-clients | eslint | — |
   | learn-ai, mitxpro, smoot-design | — | all |

   `hadolint-docker` and `shfmt-docker` run Docker, which GitHub-hosted Ubuntu runners
   provide. `packer_fmt` needs `packer` on `PATH` (`hashicorp/setup-packer`). In three
   repositories it has no files to check and could be deleted.
3. **Hook updates need a home before the uninstall.** The org preset
   (`mitodl/.github:renovate-config.json`) enables Renovate's `pre-commit` manager and has a
   custom regex manager for `prek.toml`. Renovate has opened "pre-commit hook" PRs in 15 of
   the in-scope repositories since 2026-06-01. pre-commit.ci's autoupdate is still active in
   10 repositories, and Renovate also covers 9 of them, so those 9 get duplicate bump PRs
   today. The tenth, ol-data-platform, gets hook updates only from pre-commit.ci, and needs
   Renovate coverage before the uninstall. alerting-omnibus and superset-marimo get no hook
   updates from anything today, so they needed Renovate coverage regardless. Both have since
   been dropped (§6), so neither needs it now. Dependabot plays no part:
   only access-forge uses it, and only for `pip`.
4. **`pre-commit` is called outside the hook config in three places,** and those calls change
   with the migration:
   - `lehrer/.github/workflows/ci.yml:29-31` runs `uv run pre-commit run <hook>` in the
     required `fast-checks` job.
   - `mit-learn/scripts/generate_openapi.sh:28` runs `xargs pre-commit run --files`.
   - `learn-ai/scripts/generate_openapi.sh:26` does the same.

   `django-aqueduct/.github/workflows/ci.yml:23` runs `pip install prek` with no version pin.
5. **No hook needs credentials, but five repositories have hooks or scripts that write
   generated files.** No local hook reads a secret, token or cloud credential, and
   ol-data-platform's sqlfluff uses the `jinja` templater, so it needs no warehouse
   connection. So a read-only workflow token is enough for every hook. The generated-file
   hooks are `sync-version-pins` in ol-infrastructure (writes `src/bridge/lib/version_pins/`),
   `build-config-schema` and `build-manifest-schema` in lehrer, and `uv-lock` in
   open-edx-plugins (rewrites `uv.lock`). In mit-learn and learn-ai, `generate_openapi.sh`
   runs the hooks over generated client code (finding 4). The convergence pass has to
   account for these.
6. **One constraint exists only because of pre-commit.ci.** ol-data-platform pins its
   sqlfluff hook's `dbt-core<1.12`, because 1.12's parser binary pushes the environment past
   pre-commit.ci's 250 MiB per-environment cap (comment at `.pre-commit-config.yaml:80-84`).
   The pilot can drop that cap.
7. **Manifest dependencies.** `pre-commit` is declared in the `pyproject.toml` of
   ol-infrastructure, ocw-studio, mitxonline, lehrer, ol-data-platform, ol-concourse,
   open-edx-plugins and alerting-omnibus. No `package.json` declares husky, lint-staged or
   another hook manager, so nothing competes with prek's git hook.
8. **Hook `rev`s are tags, not SHAs.** None of the 28 configurations pins a hook repository
   by commit. SHA-pinning of *actions* is in scope (issue plan step 2). Whether hook `rev`s
   follow suit (Renovate supports `rev: <sha>  # frozen: vX`) is a contract decision, not a
   prerequisite. Renovate's pre-commit manager already parses and updates that form
   ([`extract.ts`](https://github.com/renovatebot/renovate/blob/main/lib/modules/manager/pre-commit/extract.ts)).
9. **Rulesets allow bot fix commits.** Both org rulesets (`baseline-default-branch`,
   `tier-1-hardening`) apply to default branches only, and both set
   `dismiss_stale_reviews_on_push: false` and `require_last_push_approval: false`, so an
   autofix.ci commit on a PR branch neither blocks the push nor clears an approval.
   `require_extra_approval_for_unattributed_changes` only affects PRs Copilot opens without a
   human attached. `tier-1-hardening` does require review threads to be resolved. Repository
   rulesets exist in ten in-scope repositories. Required checks are `ci-gate`
   (ol-infrastructure, mitxonline, mit-learn), `openapi-diff` (mit-learn), `fast-checks` and
   `gate` (lehrer), and `test` (ol-analytics-api). None names pre-commit.ci. No repository in
   scope uses classic branch protection or a merge queue.
10. **The existing `ci-gate` jobs aggregate through `needs:`.** Repositories that have one
   (ol-infrastructure, mitxonline, mit-learn) can gate on the prek job by adding it to `needs`.
   autofix.ci requires the workflow to be named exactly `autofix.ci`, and `needs` only
   reaches jobs in the same workflow. So a prek job inside the `autofix.ci` workflow cannot
   feed an existing `ci-gate`. The contract has to decide how the prek result reaches a
   required check: require the `autofix.ci` job's own check, run prek in the gated workflow
   as well, or restructure the gate. Requiring the job's own check only works if the prek
   step's own result fails the job. The action alone exits 0 with "Nothing to do" whenever no
   file changed, so hooks that fail without modifying anything (zizmor, detect-secrets, mypy,
   actionlint, hadolint) would go green behind a `|| true` or `continue-on-error`.
11. **Two in-scope repositories may be unused.** superset-marimo was last pushed on
    2026-05-22, its hook config does not load, and it has no Renovate. alerting-omnibus was
    last pushed on 2026-06-30 and has no Renovate. Confirm with their owners before spending
    a migration PR on either. If a repository is dropped, move it to §6 with the owner's
    answer as the reason. Both were dropped on 2026-10-01 (§6).
12. **Runtimes here are not a benchmark.** `prek run --all-files` took 0.2–40 s per
   repository (mit-learn 40 s, ol-infrastructure 23 s, ol-data-platform 14 s). Those runs
   shared one hook cache four at a time, so they mix hook-environment installation with lock
   waits (the logs show cache-lock warnings). They show order of magnitude only. The pilots measure CI
   runtime directly against pre-commit.ci.

## 5. GitHub App facts and the one gap

| | pre-commit-ci (installed) | autofix.ci (to install) |
| --- | --- | --- |
| Installation id | 22207049, created 2022-01-12 | — |
| Repository selection | `selected` | Pilot: the three pilot repositories only |
| `contents` | write | write |
| `pull_requests` | write | write |
| `statuses` / `checks` | statuses: write | checks: write |
| `workflows` | **write** | — (rejects any fix touching `.github/`) |
| `actions` | — | write (cancels sibling runs when `fail-fast` is on) |
| Other | issues: read, merge_queues: read | — |
| Events | issue_comment, merge_group, pull_request, push, repository | — |

autofix.ci's permission list is from <https://autofix.ci/security>. It has no `workflows`
scope, but it adds `actions:write`. The install task should confirm what the app actually
asks for at install time.

**Installation membership.** GitHub does not report which repositories a `selected`
installation covers to an org admin's token: `GET /orgs/{org}/installations` omits the list,
and `GET /user/installations/{id}/repositories` needs a user-to-server token. The same
limitation is documented for all selected installs in
[`docs/github-app-installation-audit.md`](../github-app-installation-audit.md). An org owner
therefore read the list from
<https://github.com/organizations/mitodl/settings/installations/22207049> on 2026-09-28.
The installation covers **22 repositories**:

- **14 in scope** at measurement, the same 14 that showed a pre-commit.ci status: the 13
  "yes" rows still in §3.1 plus ocw_oer_export, now in §6. They are mit-learn,
  mit-learn-api-clients, mitxonline, mitxpro, ocw-hugo-projects, ocw-studio, ocw_oer_export,
  odl-video-service, ol-data-platform, ol-django, ol-infrastructure, ol-keycloak,
  open-edx-plugins, smoot-design. ocw_oer_export has since been dropped (§6), so 13 of them
  remain in scope. It stays in the installation until the uninstall.
- **8 archived:** concourse-packer-resource, concourse-pulumi-resource, herokuconfigurator,
  mit-open-login-button, social-auth-mitxpro, unified-ecommerce, unified-ecommerce-frontend,
  and one private archived repository. Archived repositories are read-only, so pre-commit.ci
  does nothing there, and removing it costs them nothing.

The other 14 in-scope repositories are outside the installation. Five of them (learn-ai,
lehrer, mitxonline-api-clients, ol-rootly-manager, platform-engineering-site) still carry a
`ci:` block, which nothing reads. Five of the 14 (access-forge, alerting-omnibus, hq,
ol-rootly-manager, superset-marimo) have since been dropped (§6), leaving 9.

Candidate action pins, resolved from each project's latest release on 2026-09-28. The
reference-workflow task re-resolves them.

| Action | Tag | Commit |
| --- | --- | --- |
| `autofix-ci/action` | v1.3.4 | `c5b2d67aa2274e7b5a18224e8171550871fc7e4a` |
| `j178/prek-action` | v3.0.0 | `4e14d07f9231acabce116ccfca13b13dd9755ece` |
| `actions/checkout` | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| `astral-sh/setup-uv` | v10.2.0 | `c18668ad3cf93ea998bef934396af7bb5c839dc7` |
| `actions/setup-node` | v7.0.0 | `820762786026740c76f36085b0efc47a31fe5020` |
| `hashicorp/setup-packer` | v3.4.0 | `ce93c3c08a6c2ff2275bf4b54ff0d9a75f6c9789` |

`j178/prek-action` stopped publishing moving major/minor tags at v3, so it can only be pinned
to an exact version or SHA. prek itself was at v0.5.4 on the measurement date.

## 6. Exclusions

**Dropped from scope on 2026-10-01 (6).** These were in scope at measurement, so the JSON
keeps their measurements: each has `wave: "excluded"`, its original wave in
`measured_wave`, and the reason first in `wave_reasons`. An org owner removed all six from
the autofix.ci installation (166287870) on 2026-10-01.

| Repository | Measured wave | In pre-commit.ci | Reason |
| --- | --- | --- | --- |
| access-forge | standard | no | Private. autofix.ci is paid on private repositories ([contract D7](https://github.com/mitodl/ol-github-workflows/blob/main/docs/prek-autofix-contract.md)) |
| alerting-omnibus | standard | no | Private (D7) |
| hq | standard | no | Private (D7) |
| superset-marimo | standard | no | Unused and being archived (finding 11). [superset-marimo#2](https://github.com/mitodl/superset-marimo/pull/2) closed unmerged |
| ol-rootly-manager | complex | no | Archived. [ol-rootly-manager#7](https://github.com/mitodl/ol-rootly-manager/pull/7) closed unmerged |
| ocw_oer_export | standard | **yes** | Dropped by the owner. [ocw_oer_export#230](https://github.com/mitodl/ocw_oer_export/pull/230) closed unmerged. pre-commit.ci is its only hook enforcement, and that ends at the uninstall; the owner accepted this |

**No hook configuration (42).** None of these has a `.pre-commit-config.yaml` or `prek.toml`
on its default branch, so there is nothing to migrate:

apisix-testbed, common-access, concourse-workflow, course-search-utils, default-labels-repo,
django-cas-mapper, edx-api-client, edxcut, eslint-config, gwarek, hacksnack, handbook,
keycloak-scim, micromasters, mit-moira, mitx-grading-library, mitx-theme, mitx_cas_mapper,
mitxonline-theme, mitxpro-theme, ol-customizations-nytimes-library, ol-notebook-extensions,
oldevops-scratch, open-collaboration, open-learnix, open-podcast-data,
open-resource-blocklists, open-video-data, product, realistic-mm-users, reddit-config,
redux-asserts, redux-hammock, release-script, superset-nl-explorer, tech-talk-slides,
template-mit-demo, vault-plugin-database-starrocks, vault-raft-backup, video_translation_demo,
world_geographic_regions, xanalytics.

Two of these mention pre-commit only in documentation and belong to the documentation task,
not the rollout. `open-learnix`'s README says projects' `pre-commit install` works inside its
environment, and that should be re-checked for prek. `release-script`'s AGENTS.md states it
has no pre-commit config, which is accurate.

Fifteen of the 42, plus the supporting repository ol-github-workflows, have not been pushed
in over a year (`stale_over_365_days` in the JSON). The fifteen are archive candidates for
the org-tooling task rather than this migration:
django-cas-mapper, edxcut, mit-moira, mitx_cas_mapper, oldevops-scratch, open-collaboration,
product, realistic-mm-users, reddit-config, template-mit-demo, world_geographic_regions,
xanalytics, concourse-workflow, default-labels-repo and apisix-testbed.

**Withheld (2).** Two private repositories are left out of this public document and its
JSON.

**Archived and forked repositories** (245 of the org's 322) are outside the survey by
construction.

## 7. How this was measured

Nothing below writes to GitHub.

```bash
# Scope
gh repo list mitodl --limit 1000 --json name,isArchived,isFork,pushedAt,defaultBranchRef,visibility
# Default-branch content: shallow clone of all 77, then parse .pre-commit-config.yaml, prek.toml,
# pyproject.toml / package.json / requirements*, .github/workflows, renovate.json*, docs
git clone --depth 1 --single-branch git@github.com:mitodl/<repo>.git
# Rulesets and required checks (org rulesets need the admin:org scope)
gh api repos/mitodl/<repo>/rules/branches/<default>
gh api orgs/mitodl/rulesets/<id>
gh api orgs/mitodl/properties/values
# pre-commit.ci activity: statuses on the head commit of the last 15 PRs (GraphQL), and
gh search prs --owner mitodl --author app/pre-commit-ci --created '>=2026-06-01'
# Installations
gh api orgs/mitodl/installations
# Hook compatibility, per repository, on the unmodified default branch
prek validate-config .pre-commit-config.yaml
SKIP=<local node/system hooks, packer_fmt> prek run --all-files
# ...and for any failure, the same run under pre-commit on a reset tree
SKIP=<same> pre-commit run --all-files
```

Hook runs used prek 0.5.3 and pre-commit 4.6.2 with isolated `PREK_HOME` / `PRE_COMMIT_HOME`
caches. `packer fmt -check -recursive` was run separately in `hashicorp/packer:1.14` for the
four repositories that configure `packer_fmt`.
