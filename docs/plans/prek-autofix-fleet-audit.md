# pre-commit.ci → prek + autofix.ci: post-rollout fleet audit

**Tracking issue:** [#5805](https://github.com/mitodl/ol-infrastructure/issues/5805)
**Audited:** 2026-10-02, against each repository's default branch and the live GitHub rulesets.
agent-kit and ol-analytics-api were migrated and updated the same day, after the first pass.
**Baseline:** [`prek-autofix-migration-inventory.md`](prek-autofix-migration-inventory.md), scope as updated 2026-10-01.

## Result

24 of the 28 audited repositories conform fully: the reference `autofix.yml` runs `prek`, `prek` is
a required check, and nothing from pre-commit.ci remains. The other four are explained residual
exceptions (§3); two of them were never enforced by pre-commit.ci. Nothing is unexplained, apart
from two things this audit could not verify from the API (§4).

| Check | Result |
| --- | --- |
| pre-commit.ci installed in the org | No. `orgs/mitodl/installations` lists 21 installations, no `pre-commit-ci`; `autofix-ci` (166287870) is present |
| `ci:` block left in a `.pre-commit-config.yaml` | None in the 28 audited repositories |
| Workflow still calling pre-commit, `pre-commit/action` or pre-commit.ci | None |
| Action references not pinned to a full commit SHA | None, in every workflow of the 28 repositories |
| Project manifest declaring `pre-commit` | None (`open-discussions`' `poetry.lock` mentions it only as an optional extra of other packages; there is no locked package and `pyproject.toml` does not name it) |
| `prek` required on the default branch | 24 of the 25 migrated repositories that run it from `autofix.yml`; the exception is `platform-engineering-site` (§3) |
| Required check naming pre-commit.ci | None |
| `pre-commit.ci - pr` status | Only on sampled PRs whose last push predates the uninstall (2026-10-01 ~20:45Z), for example mitxonline#4069, last pushed 19:40Z and merged 2026-10-02 |
| `pre-commit.ci` autoupdate PR opened after 2026-10-01 | None in the org (the same title search finds them in September) |
| Hook-revision update automation | Intact. The org Renovate preset keeps the `pre-commit` manager and the `prek.toml` custom manager; `ol-data-platform` has no org preset but enables the `pre-commit` manager in its own `renovate.json` |

Behavior samples:

- **Fixable.** [ol-data-platform#2783](https://github.com/mitodl/ol-data-platform/pull/2783)
  (pilot fixture, closed) carries a commit by `autofix-ci[bot]`.
- **Non-fixable.** The `autofix.yml` `prek` job fails on drift it cannot fix, for example the
  pilot fixture run [36761488461](https://github.com/mitodl/ol-data-platform/actions/runs/36761488461)
  and real PR runs such as
  [ol-infrastructure 37016838065](https://github.com/mitodl/ol-infrastructure/actions/runs/37016838065).
- **Rulesets accept the check.** The four most recent merged PRs of each audited repository were
  read: every sampled PR whose head includes the migration shows `prek` = success. Sampled PRs
  without it were merged before the migration (for example lehrer#291, merged before lehrer#290).

## 1. Repository by repository

`Req.` is whether `prek` is a required status check on the default branch. `ci:` is whether the
interim pre-commit.ci block was removed.

| Repository | Wave | Migration PR | Req. | `ci:` removal |
| --- | --- | --- | --- | --- |
| ol-infrastructure | pilot | [#6092](https://github.com/mitodl/ol-infrastructure/pull/6092) | yes ([#6145](https://github.com/mitodl/ol-infrastructure/pull/6145)) | [#6146](https://github.com/mitodl/ol-infrastructure/pull/6146) |
| ol-data-platform | pilot | [#2781](https://github.com/mitodl/ol-data-platform/pull/2781), pin [#2790](https://github.com/mitodl/ol-data-platform/pull/2790) | yes (#6145) | [#2805](https://github.com/mitodl/ol-data-platform/pull/2805) |
| smoot-design | pilot | [#264](https://github.com/mitodl/smoot-design/pull/264), pin [#268](https://github.com/mitodl/smoot-design/pull/268) | yes (#6145) | [#269](https://github.com/mitodl/smoot-design/pull/269) |
| learn-ai | complex | [#93](https://github.com/mitodl/learn-ai/pull/93) | yes ([#6127](https://github.com/mitodl/ol-infrastructure/pull/6127)) | not needed (never in pre-commit.ci) |
| lehrer | complex | [#290](https://github.com/mitodl/lehrer/pull/290) | yes ([#6132](https://github.com/mitodl/ol-infrastructure/pull/6132)) | not needed |
| mit-learn | complex | [#4027](https://github.com/mitodl/mit-learn/pull/4027) | yes (#6132) | [#4037](https://github.com/mitodl/mit-learn/pull/4037) |
| mit-learn-api-clients | complex | [#69](https://github.com/mitodl/mit-learn-api-clients/pull/69) | yes (#6132) | [#70](https://github.com/mitodl/mit-learn-api-clients/pull/70) |
| mitxonline-api-clients | complex | [#81](https://github.com/mitodl/mitxonline-api-clients/pull/81) | yes (#6132) | not needed |
| mitxpro | complex | [#4117](https://github.com/mitodl/mitxpro/pull/4117) | yes (#6132) | [#4120](https://github.com/mitodl/mitxpro/pull/4120) |
| ocw-studio | complex | [#3235](https://github.com/mitodl/ocw-studio/pull/3235) | yes (#6132) | [#3238](https://github.com/mitodl/ocw-studio/pull/3238) |
| platform-engineering-site | complex | [#88](https://github.com/mitodl/platform-engineering-site/pull/88) | **no** (§3) | not needed |
| mitxonline | standard | [#4063](https://github.com/mitodl/mitxonline/pull/4063) | yes (#6132) | [#4075](https://github.com/mitodl/mitxonline/pull/4075) |
| ocw-hugo-projects | standard | [#411](https://github.com/mitodl/ocw-hugo-projects/pull/411) | yes (#6132) | [#413](https://github.com/mitodl/ocw-hugo-projects/pull/413) |
| ocw-hugo-themes | standard | [#1879](https://github.com/mitodl/ocw-hugo-themes/pull/1879) | yes (#6132) | not needed |
| odl-video-service | standard | [#1620](https://github.com/mitodl/odl-video-service/pull/1620) | yes (#6132) | [#1621](https://github.com/mitodl/odl-video-service/pull/1621) |
| ol-concourse | standard | [#112](https://github.com/mitodl/ol-concourse/pull/112) | yes (#6132) | not needed |
| ol-django | standard | [#595](https://github.com/mitodl/ol-django/pull/595) | yes ([#6138](https://github.com/mitodl/ol-infrastructure/pull/6138)) | [#597](https://github.com/mitodl/ol-django/pull/597) |
| ol-infra-health-checks | standard | [#16](https://github.com/mitodl/ol-infra-health-checks/pull/16) | yes (#6132) | not needed |
| ol-keycloak | standard | [#307](https://github.com/mitodl/ol-keycloak/pull/307) | yes (#6132) | [#308](https://github.com/mitodl/ol-keycloak/pull/308) |
| ol-keycloakify | standard | [#210](https://github.com/mitodl/ol-keycloakify/pull/210) | yes (#6132) | not needed |
| open-discussions | standard | [#4464](https://github.com/mitodl/open-discussions/pull/4464) | yes (#6132) | not needed |
| open-edx-plugins | standard | [#880](https://github.com/mitodl/open-edx-plugins/pull/880) | yes (#6132) | [#882](https://github.com/mitodl/open-edx-plugins/pull/882) |
| ocw_oer_export | standard (re-scoped 2026-10-01) | [#235](https://github.com/mitodl/ocw_oer_export/pull/235) | yes ([#6148](https://github.com/mitodl/ol-infrastructure/pull/6148)) | not needed |
| ol-github-workflows | supporting | reference workflow and contract | n/a (§3) | n/a |
| agent-kit | already-prek | [#449](https://github.com/mitodl/agent-kit/pull/449), skills [#448](https://github.com/mitodl/agent-kit/pull/448) | yes ([#6151](https://github.com/mitodl/ol-infrastructure/pull/6151)) | not needed |
| django-aqueduct | already-prek | none needed | n/a (§3) | n/a |
| ol-analytics-api | already-prek | [#88](https://github.com/mitodl/ol-analytics-api/pull/88) | yes ([#6151](https://github.com/mitodl/ol-infrastructure/pull/6151)) | not needed |
| .github | supporting | none needed (hosts the Renovate preset) | n/a (§3) | n/a |

## 2. Out of scope, and why it still holds

- **access-forge, alerting-omnibus, hq.** Still private. autofix.ci is paid on private repositories
  (contract D7), so they stay out. Each keeps a `.pre-commit-config.yaml` that nothing runs in CI.
- **superset-marimo, ol-rootly-manager.** Archived on GitHub (see §5 for their Pulumi state).
- **pre-commit-ci-config.** Unused 2023 fork, archived by
  [#6147](https://github.com/mitodl/ol-infrastructure/pull/6147).
- **ol-llm.** Created 2026-10-02, outside the inventory. It has `.github/workflows/autofix.yml`
  and a `.pre-commit-config.yaml`. It was not audited here.
- **Two security-advisory fork repositories** (`mit-learn-ghsa-*`, `ocw-studio-ghsa-*`) carry a
  `.pre-commit-config.yaml` copied from their parent. They are temporary private forks.
- Every other active repository without a hook configuration was re-checked for a
  `.pre-commit-config.yaml`, `prek.toml` or autofix/pre-commit workflow: none appeared since the
  inventory.

## 3. Residual exceptions

1. **platform-engineering-site: no hook enforcement, by owner decision (2026-10-01).** Its
   migration PR merged, but GitHub Actions is disabled for the repository
   (`actions/permissions` → `enabled: false`), so `autofix.yml` has never run and `prek` is not
   required. It is a documentation site; the owner declined to enable Actions.
2. **.github: no hook configuration.** It hosts the Renovate preset only.
3. **django-aqueduct and ol-github-workflows: `prek` runs in CI but is not a required check.**
   django-aqueduct runs it from its own `ci.yml`, ol-github-workflows from `autofix.yml`. Neither
   was part of the cutover tasks.
4. **Open PRs that predate the migration show `prek` as pending until updated.** Required checks
   were applied without gating on in-flight PRs (owner decision, 2026-10-01). When each was
   applied, `blocked` named: ol-infrastructure 15, ol-data-platform 9, smoot-design 9 of 10,
   ocw_oer_export 1 (#217, a stale pre-commit.ci autoupdate), agent-kit 5 (3 Renovate PRs,
   [#350](https://github.com/mitodl/agent-kit/pull/350) and
   [#327](https://github.com/mitodl/agent-kit/pull/327)) and ol-analytics-api 5
   ([#87](https://github.com/mitodl/ol-analytics-api/pull/87),
   [#82](https://github.com/mitodl/ol-analytics-api/pull/82),
   [#81](https://github.com/mitodl/ol-analytics-api/pull/81),
   [#69](https://github.com/mitodl/ol-analytics-api/pull/69),
   [#36](https://github.com/mitodl/ol-analytics-api/pull/36)). About 176 default-branch PRs across
   13 other repositories were in the same state on 2026-10-01.
5. **ol-data-platform keeps a `dbt-core<1.12` cap** on the sqlfluff hooks. It only existed for
   pre-commit.ci's per-environment size limit. [ol-data-platform#2807](https://github.com/mitodl/ol-data-platform/pull/2807)
   lifts it and is open; it does not block this audit.
6. **Two hooks check nothing in CI, found while migrating agent-kit and ol-analytics-api.** A
   migration PR does not change hooks (contract §2), so both were left as they were:
   - `gitleaks` runs `--staged` in both repositories. Nothing is staged in the `prek` job, so a
     committed secret passes. Confirmed by committing a Slack-format token in a scratch clone.
   - `markdownlint` in agent-kit passes `--disable MD013 MD033`, which is variadic and swallows the
     filenames, so the CLI prints its usage and exits 0.

   Other repositories with the same hook definitions may have the same gap. That was not checked.

## 4. Not verified by this audit

- **autofix.ci repository membership.** The REST API does not return an installation's repository
  list to this token (HTTP 403, same as audit finding 4). The intended set is the 28 audited
  repositories minus the ones with no `autofix.yml`; the six dropped on 2026-10-01 were removed and
  `ocw_oer_export` was re-added on 2026-10-02, both by an org owner in the UI. On 2026-10-02 the
  owner also reported that `agent-kit` and `ol-analytics-api` are in the installation, which this
  audit takes from the owner's statement and cannot read back. An org owner should confirm the list
  once in Settings → GitHub Apps → autofix.ci.
- **Fixable-path behavior on a real PR.** The only commit by `autofix-ci[bot]` found is on the
  closed pilot fixture. No post-migration PR needed an automated fix in the samples read.

## 5. Related fix

The Concourse `pulumi-github-repositories` pipeline failed on `pulumi refresh` from 2026-10-01
14:13 EDT because the stack still held `RepositoryVulnerabilityAlerts` for the now-archived
`superset-marimo` and `ol-rootly-manager`. Four stale state entries were removed (state-only, no
GitHub change) and deploy #54 applied the pending ruleset and archival changes on 2026-10-02.
