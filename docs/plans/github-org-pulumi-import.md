# Importing the `mitodl` GitHub Organization into Pulumi

**Witan project:** `wp-import-the-mitodl-github-organization-into-pulum-47211a`
**Status:** phase 0 complete (App widened and verified); phase 1 tooling landed —
`bin/github-org-inventory`. Phase 2 gated on nothing but authoring.
**Provider:** `pulumi-github>=6.0.0,<7` — resource names and import IDs verified against the
shipped **6.14.1** schema on 2026-08-03 (§5.2)
**Auth:** GitHub App installation `ol-infrastructure-as-code` (id `150389158`), wired via
`ol_infrastructure.lib.github_helper.setup_github_provider()`

---

## 1. Baseline: what we are importing

Originally hand-sampled 2026-07-31. **Superseded 2026-08-03 by a full crawl** —
`uv run bin/github-org-inventory report --refresh`, 316 repos in ~60s. The corrected
numbers are in §1.1; the table immediately below is the original estimate, kept because
several of its figures were extrapolated from a five-repo sample and it is useful to see
which way the extrapolation erred.

| Dimension | Count | Notes |
|---|---:|---|
| Repositories (total) | 317 | |
| — public, active | 168 | the working fleet |
| — public, archived | 131 | read-only; import shape must differ |
| — private, active | 11 | |
| — private, archived | 7 | |
| Teams | 14 | 3 have a parent (nested) |
| Org members | 39 | 39/39 seats filled on the Team plan |
| App installations | 21 | third-party; **not** Pulumi-managed, but audited |
| Org custom properties | 0 | greenfield — the classification backbone |

Per-repo sub-resource fan-out is wildly uneven, which drives the scoping rules in §4:

| Repo | Environments | Actions secrets | Webhooks | Teams | Direct collaborators |
|---|---:|---:|---:|---:|---:|
| `mitxonline` | **412** | 3 | 2 | 2 | 1 |
| `mit-learn` | 44 | 6 | 1 | 3 | 0 |
| `ol-infrastructure` | 2 | 1 | 1 | 4 | 0 |

The 412 and 44 are ephemeral Heroku/review-app deployment environments created by CI.
**They must not enter Pulumi state** — see §4.3.

### 1.1 Measured baseline (full crawl, 2026-08-03)

| Dimension | Sampled estimate | **Measured** |
|---|---:|---:|
| Repositories | 317 | **316** |
| — active | 179 | **178** |
| — archived | 138 | **138** |
| — public / private | 299 / 18 | **299 / 17** |
| — `fork` flag set | ~60 | **115** |
| Teams / members | 14 / 39 | **14 / 39** |
| Org custom properties | 0 | **0** |
| Org rulesets | — | **0** |
| **Environments, org-wide** | ~460 assumed | **7,803** |

Three corrections worth carrying forward:

- **Environment sprawl is an order of magnitude worse than assumed.** The sample found
  `mitxonline` (412) and `mit-learn` (44) and treated those as the extremes. They are not
  close to it: `micromasters` holds **2,694**, `mitxpro` **2,042**, `open-discussions`
  **1,723** — and all three were pushed to within the last two weeks, so these are live
  repos accumulating review apps, not abandoned ones. 7,803 environments against a fleet
  of 178 active repos is roughly 44 per repo. §4.3's rule (import environments only from
  an explicit per-repo allowlist) was already right; this makes it load-bearing rather
  than prudent.
- **The fork population is ~115, not ~60.** Nearly double. Since §9.2 decided to import
  forks as `tier: unmanaged`, this changes the size of the inert tail but not the
  approach — and it roughly doubles the number of false findings that misclassification
  would have produced.
- **102 of 178 active repos still default to `master`.** Not previously measured.
  Concentrated in the fork fleet, where it is upstream's choice and should stay.
  **Resolved 2026-08-05:** forks and archived repos are exempt from CON-03 and the `fork`
  archetype no longer inherits `default_branch` (§3.4). The actionable set is **25**.

Merge-strategy spread across active repos, which is CON-01's real shape:

| Configuration | Repos |
|---|---:|
| squash + merge + rebase (all three) | 158 |
| squash only | 19 |
| squash + rebase | 1 |

The sample's impression that "merge strategy is per-repo improvisation" was directionally
right but structurally wrong: it is not 178 bespoke configurations, it is **one dominant
default (all three, 89%) plus a 20-repo tail** that someone deliberately tightened.

**Resolved 2026-08-05: merge strategy is not enforced.** Squash-merge remains the general
preference, but pinning it in the archetype would have made CON-01 a fleet-wide behaviour
change rather than a cleanup of outliers. Each repo records its own values verbatim and
CON-01 is informational. See §3.4 for the mechanism and for how to turn enforcement on
later with the rollout diff visible up front.

### Drift already visible without any tooling

These are not hypotheses; they came out of a five-repo sample and the org settings object.

- **Merge strategy is per-repo improvisation.** `mit-learn` squash-only; `mitxonline`
  squash-only; `ol-django` squash+rebase; `ol-infrastructure` and `hq` allow all three
  (including merge commits).
- **`ol-django` has no branch protection and no ruleset at all.** Anyone with write can
  push straight to `main`.
- **Zero sampled repos require any status check.** Where protection exists it is
  `required_approving_review_count: 1` and nothing else — CI is advisory everywhere.
- **Zero repos require signed commits.**
- **Dependabot security updates are disabled** on every sampled repo, and the org default
  for new repos disables `advanced_security`, `dependency_graph`, and `dependabot_alerts`.
- **Rulesets are nearly unused** — only a "Copilot review for default branch" ruleset on
  two repos.
- **`hq` (private) has secret scanning and push protection off** while every sampled public
  repo has them on — the inverse of the risk gradient you would want.
- Org-level: `members_can_change_repo_visibility: true` and
  `members_can_create_public_repositories: true` — any of 39 members can make a private
  repo public. Counterweights that *are* set correctly: `default_repository_permission: none`,
  `members_can_delete_repositories: false`, `two_factor_requirement_enabled: true`.

This drift is the justification for the whole project, and §7 turns finding it into a
repeatable job rather than a one-off sample.

---

## 2. Blocking prerequisite: the GitHub App permission set

The `ol-infrastructure-as-code` installation currently holds exactly:

```
metadata:read, repository_hooks:write
```

That was sufficient for its one job (the OCW Studio webhook, `applications/ocw_studio/__main__.py:351`).
It is nowhere near enough to read, let alone manage, the org. **Nothing else in this plan can
start until the app is widened.**

### 2.1 Required permissions

**The authoritative list now lives in `docs/github-app-permissions.md`** — it carries both the
UI label and the API slug for each entry, the verification provenance, and the two unresolved
items. Grant from that file, not from the summary below.

Three entries in the first draft of this section were wrong and would have caused a failed
grant: `custom_properties` is really `repository_custom_properties` /
`organization_custom_properties`; `actions_variables` does not exist as a slug at all; and the
Dependabot-alerts *toggle* is `administration:write`, not `vulnerability_alerts:write` (that
permission covers reading alerts). They were caught by checking GitHub's own OpenAPI
`app-permissions` vocabulary and the live installation data rather than by re-reading the
table. The tables below are corrected.

Grouped by what they unlock. `A` = required for the import to even read state; `B` = required
to manage the resource type; `C` = audit-only (read) — grant these too, they are what makes §7
work without a separate PAT.

#### Repository permissions

| Permission | Level | Tier | Unlocks |
|---|---|:--:|---|
| `metadata` | read | A | Mandatory for every app. Repo listing. |
| `administration` | write | B | `Repository`, `RepositoryTopics`, `BranchDefault`, `BranchProtection`, `RepositoryRuleset`, `RepositoryDeployKey`, `RepositoryAutolinkReference`, `RepositoryCollaborators`, `TeamRepository`, `RepositoryVulnerabilityAlerts`, `RepositoryDependabotSecurityUpdates`, security-and-analysis toggles. **This is the big one.** |
| `contents` | write | B | `RepositoryFile`, `Branch`, `Release`, `RepositoryMilestone` bodies. Needed to push CODEOWNERS/SECURITY.md fleet-wide in the remediation phase. |
| `workflows` | write | B | Any `RepositoryFile` under `.github/workflows/`. Required if we standardize CI files. |
| `secrets` | write | B | `ActionsSecret` (repo scope). |
| *"Variables"* (slug unconfirmed) | write | B | `ActionsVariable`. Out of scope until the slug is confirmed at grant time. |
| `dependabot_secrets` | write | B | `DependabotSecret`. |
| `environments` | write | B | `RepositoryEnvironment` — only for the curated allowlist (§4.3). |
| `issues` | write | B | `IssueLabel`, `IssueLabels`, `RepositoryMilestone`. Label standardization. |
| `pull_requests` | read | C | Audit: merge-queue and review-policy checks. |
| `repository_hooks` | write | B | `RepositoryWebhook`. Already held. |
| `pages` | write | B | `RepositoryPages` — only if we manage Pages. Optional. |
| `vulnerability_alerts` | read | C | Audit: which repos have open Dependabot alerts. The alerts on/off **toggle** is `administration:write`, not this. |
| `secret_scanning_alerts` | read | C | Audit: open secret-scanning alerts per repo. |
| `security_events` | read | C | Audit: code-scanning alert state. |
| `deployments` | read | C | Audit: which environments are actually live vs. abandoned. |
| `repository_custom_properties` | read | C | Reads a repo's own property values. **Setting** them is `organization_custom_properties`, below. |

#### Organization permissions

| Permission | Level | Tier | Unlocks |
|---|---|:--:|---|
| `members` | write | B | `Membership`, `Team`, `TeamMembership`, `TeamMembers`, `TeamSettings`, `TeamRepository`. |
| `organization_administration` | write | B | `OrganizationSettings`, `OrganizationRuleset`, org Actions policy. |
| `organization_hooks` | write | B | `OrganizationWebhook`. |
| `organization_custom_properties` | admin | B | `OrganizationCustomProperties` (schema) **and `RepositoryCustomProperty`** (setting values on repos). `admin` is needed to define the schema. The backbone of §5.4. |
| ~~`organization_custom_roles`~~ | — | — | **Withdrawn 2026-08-03.** Custom *repository* roles are Enterprise-only; `GET /orgs/mitodl/custom-repository-roles` returns 404 "Feature not available for the mitodl organization" on the Team plan. `OrganizationCustomRole` / `OrganizationRepositoryRole` are unusable here. Distinct from `organization_custom_org_roles`, which works. |
| `organization_custom_org_roles` | write | B | `OrganizationRole`, `OrganizationRoleTeam`, `OrganizationRoleUser`. |
| `organization_secrets` | write | B | Org-level `ActionsSecret`, `DependabotOrganizationSecret`. |
| *"Variables"* (slug unconfirmed) | write | B | `ActionsOrganizationVariableRepository`. Same unresolved slug as the repo-level entry. |
| `organization_self_hosted_runners` | write | B | `ActionsRunnerGroup`. |
| `organization_user_blocking` | write | B | `OrganizationBlock`. |
| `organization_plan` | read | C | Audit: seat count vs. member count (currently 39/39 — no headroom). |
| `organization_personal_access_tokens` | read | C | Audit: fine-grained PATs with org access. |
| `organization_personal_access_token_requests` | read | C | Audit: pending PAT requests. |
| `organization_events` | read | C | Audit: correlate drift with who changed what. |

(An earlier version of this table also listed `organization_projects: read`. It is **excluded** —
`docs/github-app-permissions.md` lists it under "Deliberately excluded", and that file is
authoritative. Granting it would make SEC-12 fire on our own installation. Corrected 2026-08-03.)

**Deliberately excluded:** `organization_copilot_seat_management`, `codespaces*`,
`organization_packages`, `team_discussions`, `emu_group_mapping`. Add later if scope grows —
each one is a new blast-radius surface for no current benefit.

### 2.2 Safety counterweights for `administration: write`

`administration: write` permits repository deletion. Three mandatory mitigations, all in code:

1. Every `github.Repository` carries `pulumi.ResourceOptions(retain_on_delete=True)`. Removing
   a repo from the YAML removes it from state and *never* from GitHub.
2. Org-level resources (`OrganizationSettings`, `OrganizationRuleset`, `OrganizationWebhook`)
   carry `protect=True`. **So does `Membership`** — deleting one removes a human from the org,
   which is the same class of irreversible action as deleting a repo and was missing from the
   first version of this list (see §9.3).
3. The Concourse job for the `github-organization` project requires manual approval before
   `pulumi up`; `github-repositories` may auto-apply once the empty-diff gate (§6) is green.

### 2.3 Deliverable — **done**

`docs/github-app-permissions.md` is written: both UI label and API slug per entry, a "why"
per line tied to the Pulumi resources it unlocks, the deliberate exclusions, the safety
counterweights, and a provenance section recording how each entry was verified and which
three were wrong in the first draft.

Audit rule SEC-12 diffs the live installation (`GET /orgs/mitodl/installations`) against that
file. We audit 21 third-party installations for excessive scope; ours is held to the same
standard.

---

## 3. Code layout

### 3.1 Two Pulumi projects, split by blast radius

```
src/ol_infrastructure/substructure/github/
├── organization/                 # project: ol-substructure-github-organization
│   ├── Pulumi.yaml
│   ├── Pulumi.Production.yaml
│   ├── __main__.py
│   ├── org_settings.py
│   ├── custom_properties.py
│   ├── teams.py
│   ├── members.py
│   ├── org_rulesets.py
│   └── org_automation.py         # org webhooks, runner groups, org secrets/vars
└── repositories/                 # project: ol-substructure-github-repositories
    ├── Pulumi.yaml
    ├── Pulumi.Production.yaml
    ├── __main__.py
    ├── archetypes.py
    ├── repository.py             # the per-repo resource factory
    ├── rulesets.py
    ├── automation.py             # per-repo webhooks, secrets, curated environments
    └── data/
        ├── archetypes.yaml
        └── repos/
            ├── mit-learn.yaml
            ├── ol-django.yaml
            └── …                 # one file per repo, 317 of them
```

Single stack name `Production` in both — a GitHub org has no environments. This matches the
short-stack-name convention in `src/ol_infrastructure/AGENTS.md`.

**Why two projects and not one.** ~1,800–2,200 resources in a single stack means a refresh
touching every one of them; previews run in the 10-minute range and every trivial topic change
drags org settings through the same plan. Splitting puts the 39 members / 14 teams / handful of
org toggles — the things where a mistake locks people out of the org — in a stack that previews
in under a minute and gets human review, while the mechanical repo fleet iterates freely.

**Why not more than two.** The only cross-cutting resource is `TeamRepository`, which needs both
a team and a repo. `pulumi-github` accepts a **team slug** for `team_id`, so `repositories` can
reference `odl-engineering` as a plain string — no `StackReference`, no output plumbing. The
only coupling left is deploy order (organization before repositories), which the pipeline
encodes. A third split would start requiring real stack references for no gain.

### 3.2 Repositories are data, not code

317 hand-written Python blocks is not maintainable and not auditable. The repo fleet is
described by YAML validated with Pydantic (the repo's existing config convention), and the
Python is a loop.

`data/archetypes.yaml` — the shared shapes:

```yaml
archetypes:
  base:                                  # every non-archived repo inherits this
    tier: standard                       # -> custom property; org rulesets target this
    delete_branch_on_merge: true
    allow_auto_merge: true
    # Merge strategy deliberately NOT pinned -- see §3.4.
    has_issues: true
    has_wiki: false
    web_commit_signoff_required: true
    vulnerability_alerts: true
    dependabot_security_updates: true
    secret_scanning: enabled
    secret_scanning_push_protection: enabled
    teams:
      odl-engineering: push
      odl-engineering-owners: admin
    # NOTE: no branch-protection block here. Since C6c, protection is enforced by
    # org rulesets targeting `tier` (§5.4), not by a per-repo ruleset resource.
    # Only `required_status_checks` stays per-repo -- see §5.4 and DX-04.

  application:                           # deployed services: mit-learn, mitxonline, …
    extends: base
    tier: tier-1
    visibility: public

  library:                               # ol-django, smoot-design, published packages
    extends: base
    tier: tier-1

  infrastructure:                        # ol-infrastructure, ol-concourse, dagster
    extends: base
    tier: tier-1
    teams:
      devops: push
      odl-engineering-owners: admin

  fork:                                  # upstream forks: edx-platform, XBlock, tutor, …
    extends: base
    tier: unmanaged                      # no org ruleset targets this tier
    has_issues: false
    default_branch: null                 # explicitly unenforced -- see §3.4

  archived:                              # the 138 archived repos
    archived: true
    # nothing else — GitHub rejects most writes to archived repos
```

`data/repos/mit-learn.yaml` — only the deviations:

```yaml
name: mit-learn
archetype: application
description: MIT Learn — course and resource discovery
topics: [django, react, learn]
custom_properties:
  owning_team: mit-learn
  data_classification: internal
teams:
  arbisoft-contractors: push
required_status_checks:                  # the one genuinely per-repo rule; see §5.4
  - "javascript-tests"
  - "python-tests"
```

`repository.py` resolves `archetype → deviations → resource kwargs` and emits the resource
family. Every field a repo does *not* declare is inherited, so `git diff` on the data directory
shows exactly the drift and nothing else. This is also what makes §7 cheap: the audit reads the
same YAML through the same Pydantic model, so "what does this repo deviate on" is a
dictionary comparison rather than an API crawl.

### 3.3 Modularity by resource type

| Module | Resource types | Cardinality |
|---|---|---:|
| `organization/org_settings.py` | `OrganizationSettings` | 1 |
| `organization/custom_properties.py` | `OrganizationCustomProperties` | **1 per property** (~3), not 1 total — see §5.2 |
| `organization/teams.py` | `Team`, `TeamSettings`, `TeamMembership` | ~14 + ~60 |
| `organization/members.py` | `Membership` | 39 |
| `organization/org_rulesets.py` | `OrganizationRuleset` (property-targeted, §5.4) | ~4 |
| `organization/org_automation.py` | `OrganizationWebhook`, `ActionsRunnerGroup`, org secrets/variables | ~10 |
| `repositories/repository.py` | `Repository`, `RepositoryTopics`, `BranchDefault`, `RepositoryVulnerabilityAlerts`, `RepositoryDependabotSecurityUpdates`, `RepositoryCustomProperty`, `TeamRepository` | 317 × ~6 |
| `repositories/rulesets.py` | `RepositoryRuleset` — only repos declaring `required_status_checks` (§5.4) | ~20, not 179 |
| `repositories/automation.py` | `RepositoryWebhook`, `ActionsSecret`, `ActionsVariable`, `RepositoryEnvironment` (allowlist) | ~250 |

---

### 3.4 Unenforced-but-managed fields

Two settings were deliberately left unpinned on 2026-08-05, once the full crawl showed what
enforcing them would actually cost. Both use the same mechanism, and the distinction it
encodes is worth stating plainly: **an archetype declining to pin a field is not the same as
the field going unmanaged.**

| Field | Decision |
|---|---|
| `allow_squash_merge` / `allow_merge_commit` / `allow_rebase_merge` | **Unenforced.** Squash-merge is the general preference, but 158 of 178 active repos allow all three. Pinning squash-only would make CON-01 a fleet-wide behaviour change affecting ~89% of active repos rather than a cleanup of outliers. |
| `default_branch` on the `fork` archetype | **Exempt.** 102 active repos default to `master` and nearly all are upstream forks, where the branch name is upstream's choice. CON-03 skips forks and archived repos for the same reason; the actionable set drops from 106 to **25**. |

The mechanism: a key set to `null` in a child archetype means *explicitly not enforced* and
is dropped rather than inherited (`fork` uses this for `default_branch`). Where the resolved
archetype has no opinion on a field Pulumi will manage, the per-repo YAML records that
repo's **current value verbatim**. The field stays managed and the empty-diff gate still
holds — the repo is simply not being asked to change.

Getting this wrong in either direction is a real failure. Pin the field and you have
silently authored a fleet-wide change. Drop it entirely and the provider applies its own
default on the first `up`, which is a change nobody reviewed at all.

Concretely, 265 of 316 repos now carry `default_branch`: 102 forks and 138 archived repos
(neither archetype pins it) plus the 25 non-fork active repos genuinely on `master`, which
is exactly the CON-03 backlog.

**To start enforcing merge strategy later**, add the three `allow_*_merge` keys back to
`base`. The per-repo files immediately show the 158 repos that would change, and that diff
*is* the rollout plan — review it before applying rather than discovering it in a preview.

## 4. Scoping rules — what stays out

The single most common way a brownfield IaC import fails is importing things that are actually
managed by something else, then fighting a permanent diff.

### 4.1 Secrets: names only, values never

The resource *is* importable (`<repo>:<secret-name>`, verified §5.2) — an earlier draft of this
plan claimed otherwise. What is not importable is the **value**: GitHub never returns it, and
the provider explicitly documents that `value` / `encrypted_value` / `plaintext_value` are left
unpopulated in state after an import.

That is worse than being unimportable, because it fails quietly. An imported `ActionsSecret`
carries a name and no value, so the very next preview wants to write a value — which breaks the
§6 empty-diff gate, and breaks it in the one way that is tempting to wave through ("it's just
the secrets"). Waving it through would push a wrong or empty value to a live secret. So the
rule stands, on firmer ground: **do not import Actions or Dependabot secrets at all.** Same
reasoning covers `DependabotSecret` and the org-level secret resources.

Two consequences:

- The inventory records **secret names and `updated_at` per repo** and stops there. That
  inventory is itself a security finding source (§7 SEC-09: secrets never rotated).
- Bringing secret *values* into Pulumi means sourcing them from Vault
  (`bridge.secrets.sops` / the existing Vault provider) and writing them out. That is a
  genuinely useful fast-follow but it is a **migration**, not an import — it changes the
  source of truth. Keep it out of phases 1–3 entirely.

### 4.2 Third-party app installations

The 21 installed apps (Renovate, GitGuardian, Codecov, pre-commit-ci, Sentry, Claude, …) are
configured in their own vendor consoles. Pulumi can manage `AppInstallationRepository`
(which repos an install can see) but not the app's permissions. Import the repo scoping only
if we want to control it; otherwise leave the whole surface out and cover it with an audit rule
(§7 SEC-11), which is where the value is.

### 4.3 Ephemeral deployment environments

`mitxonline` has 412 environments and `mit-learn` 44 — Heroku review apps and per-PR deploys
created and destroyed by CI. Importing them guarantees a permanently churning diff.

Rule: `RepositoryEnvironment` is imported **only** from an explicit per-repo allowlist in the
repo's YAML (`environments: [production, staging]`). Everything else is ignored. The inventory
reports the excluded count per repo so the decision stays visible rather than silent.

### 4.4 Archived repositories

All 138 archived repos are imported, but with the `archived` archetype — `archived: true` and
nothing more. Rationale: leaving them out of state means nothing stops someone re-adding them
later with a full config and triggering a wave of failed writes against read-only repos.
Importing them as inert makes their status explicit and gives the audit something to check
(§7 CON-07: archived repos that still grant write access to a team).

### 4.5 Forks of upstream projects

A large share of the public fleet is upstream Open edX forks (`edx-platform`, `XBlock`,
`tutor`, `frontend-app-*`, `openedx-events`, …). These follow upstream conventions, not ours.
The `fork` archetype exists so the audit does not generate 60 false "missing CODEOWNERS"
findings. Classifying every repo into an archetype is the manual step of phase 1 and it is
the step that determines whether §7 produces signal or noise.

---

## 5. Discovery and import mechanics

### 5.1 One inventory pass produces both sides

The core technique: a single crawl of the org emits **both** the YAML data files **and** the
Pulumi bulk-import file. Code and state therefore agree by construction — the usual failure
mode, where hand-written code drifts from imported state and the first preview is a wall of
diffs, cannot occur.

`bin/github-org-inventory` (cyclopts, per repo convention):

```
github-org-inventory crawl   --out src/.../repositories/data/  # YAML data files
github-org-inventory imports --out import-repositories.json    # pulumi import -f payload
github-org-inventory infer-archetypes                          # cluster repos, propose archetype per repo
github-org-inventory report                                    # raw estate report, pre-Pulumi
```

`infer-archetypes` clusters by observed settings and proposes an archetype per repo; a human
confirms. It is the difference between a two-hour and a two-week phase 1.

### 5.2 Bulk import

`pulumi import -f import-repositories.json --generate-code=false --yes`. Code generation is
off because we author from the same inventory; generated code would fight the archetype model.

Import IDs by resource type. **Verified 2026-08-03 against the shipped provider schema**, not
against docs or memory:

```bash
pulumi package get-schema github@6.14.1   # then read each resource's "## Import" section
```

`pulumi-github` 6.14.1 is what `pyproject.toml` pins (`>=6.0.0,<7`) and what the lockfile
resolves. Re-run the command above after any provider bump — the separator characters are not
stable across majors, and three entries in the pre-verification draft of this table were wrong.

| Resource | Import ID form | Notes |
|---|---|---|
| `Repository` | `<repo>` | |
| `RepositoryTopics` | `<repo>` | |
| `BranchDefault` | `<repo>` | |
| `RepositoryRuleset` | `<repo>:<ruleset-id>` | |
| `OrganizationRuleset` | `<ruleset-id>` | |
| `BranchProtection` | `<repo>:<pattern>` | |
| `Team` | `<team-id>` **or** `<team-slug>` | both accepted |
| `TeamRepository` | `<team-id>:<repo>` **or** `<team-slug>:<repo>` | slug form is what §3.2 relies on |
| `TeamMembership` | `<team-id>:<username>` **or** `<team-slug>:<username>` | |
| `TeamMembers` | `<team-id>` | **use the numeric id.** The provider warns that importing by slug makes it convert slug↔id and *destroy and recreate every membership*. |
| `TeamSettings` | `<team-id>` or `<team-slug>` | |
| `Membership` | `<org>:<username>` | |
| `RepositoryWebhook` | `<repo>/<hook-id>` | **slash**, not colon |
| `OrganizationWebhook` | `<hook-id>` | |
| `RepositoryVulnerabilityAlerts` | `<repo>` | |
| `RepositoryDependabotSecurityUpdates` | `<repo>` | |
| `RepositoryEnvironment` | `<repo>:<env>` | a literal `:` inside an env name escapes as `??` |
| `RepositoryDeployKey` | `<repo>:<key-id>` | |
| `RepositoryAutolinkReference` | `<repo>/<id>` or `<repo>/<key-prefix>` | **slash** |
| `IssueLabels` | `<repo>` | |
| `OrganizationSettings` | `<org-id>` (numeric) | |
| `OrganizationCustomProperties` | `<property-name>` | one resource **per property** — see below |
| `RepositoryCustomProperty` | `<org>:<repo>:<property-name>` | **three parts, and the org name is part of the id** |
| `ActionsSecret` | `<repo>:<secret-name>` | importable — but see §4.1, we still do not |
| `ActionsVariable` | `<repo>:<variable-name>` | |
| `DependabotSecret` | `<repo>:<secret-name>` | |
| `ActionsOrganizationSecret` | `<secret-name>` | |
| `ActionsOrganizationVariable` | `<variable-name>` | |
| `ActionsRunnerGroup` | `<runner-group-id>` | |
| ~~`OrganizationCustomRole`~~ | ~~`<role-id>`~~ | Enterprise-only — unusable on the Team plan (§2.1) |
| `OrganizationRole` | `<role-id>` | |
| `AppInstallationRepository` | `<installation-id>:<repo>` | only if §4.2 is ever brought in scope |
| `RepositoryCollaborators` | **undocumented** | the schema carries no `## Import` section for this resource. Treat as unimportable until proven otherwise on a scratch stack; §7 SEC-06 covers collaborators as an audit rule regardless. |

Two corrections this verification produced that change the plan, not just the table:

1. **`ActionsSecret` *is* importable** (`<repo>:<secret-name>`). The earlier "not importable"
   claim was wrong. It does not change the §4.1 decision — see the rewritten §4.1 for why the
   real blocker is the empty-diff gate, not importability — but the reasoning had to be fixed,
   because "impossible" and "possible but a bad idea" lead to different designs.
2. **`OrganizationCustomProperties` is one resource per property, despite the plural name.**
   Its only required input is `propertyName`. §3.3's cardinality of 1 was wrong; it is one
   resource per property in the schema (`tier`, `owning_team`, `data_classification` → 3).
   Relatedly, `RepositoryCustomProperty` requires `property_type` alongside the value, so the
   archetype resolver must carry each property's type from the schema definition rather than
   reading a bare value out of the repo YAML.

### 5.3 Batching and rate limits

A GitHub App installation on an org gets 15,000 requests/hour, which comfortably covers a
~2,000-resource refresh. The real constraint is wall-clock: import in batches of ~25 repos,
running the §6 gate after each batch. A failure in batch 4 is then 25 repos to reason about,
not 317.

---

### 5.4 Branch protection comes from org rulesets, not per-repo resources

Settled empirically by C6c (§9.1). A `repository_property` condition on an organization
ruleset genuinely matches: the labeled repo saw the ruleset, the unlabeled control did not,
and both read endpoints agreed.

So the baseline is **~4 `OrganizationRuleset` resources targeting a `tier` custom property**,
not ~179 `RepositoryRuleset` resources:

| Ruleset | Targets | Enforces |
|---|---|---|
| `baseline-default-branch` | `tier in (tier-1, standard)` | no force-push, no deletion, 1 approving review, dismiss stale reviews |
| `tier-1-hardening` | `tier = tier-1` | require last-push approval, require conversation resolution |
| `archived-freeze` | `tier = archived` | block all writes |
| *(none)* | `tier = unmanaged` | forks are deliberately untargeted |

What this buys:

- The repositories stack drops ~179 resources, and the baseline becomes reviewable as four
  objects rather than a diff across 179 near-identical ones.
- Tightening the baseline is a one-line change to one ruleset, not a 179-repo rollout.
- A new repo is protected the moment its `tier` property is set — no separate ruleset
  resource to remember, which removes the "someone created a repo and it had no protection"
  failure mode entirely (currently the state of `ol-django`).
- Because C7 passed, each ruleset can be introduced at `enforcement: evaluate`, watched, then
  promoted to `active`.

**`required_status_checks` stays per-repo.** Check names differ per repo (`javascript-tests`
vs `python-tests`), so that one rule remains a small `RepositoryRuleset` on repos that declare
it. Everything else is org-level. This is also why DX-02/DX-03 stay per-repo audit rules.

**Two ordering consequences.** Property *values* must exist before a ruleset can match them:

1. **Custom properties move from fast-follow to prerequisite.** CON-09 is no longer an audit
   finding to fix later; populating the `tier` property is part of the import.
2. **A cross-project ordering edge appears.** The schema and rulesets live in `organization`;
   the per-repo values live in `repositories`. Deploy order is organization → repositories,
   after which the rulesets begin matching. The failure mode is safe by construction — an
   unlabeled fleet matches *nothing* rather than everything — but the pipeline must encode
   the order rather than discover it.

This does not require a `StackReference`: the ruleset names a property by string, and the
repositories stack sets that property's value by string. The coupling is a shared vocabulary,
not a Pulumi output. Keep the `tier` allowed-values list in one module imported by both
projects so a typo fails at plan time instead of silently un-targeting a repo.

**Retain the per-repo fallback.** C9 confirmed repo-level rulesets work, so if a specific repo
ever needs to escape the org baseline, `RepositoryRuleset` remains available. Do not delete
that code path from the archetype model.

## 6. The empty-diff gate

**Non-negotiable, and the thing that makes the rest of the plan safe.**

After each import batch, `pulumi preview` must report **zero changes**. Not "only harmless
changes" — zero. If it does not, the discrepancy is either a bug in the archetype resolution
or a real setting the inventory failed to capture, and both must be fixed before proceeding.

This enforces the separation that brownfield imports usually blur:

- **Phases 1–3 capture reality.** Pulumi learns what exists. Nothing on GitHub changes.
- **Phases 4–5 change reality.** Only after the model provably matches the world.

An import that quietly "fixes things on the way in" gives you a wave of unreviewed production
changes attributed to an import commit, and no way to tell an intentional change from a
modelling bug. Practical rule: if a batch's preview is non-empty, encode the current value as
a deviation in that repo's YAML — even when the current value is wrong. Making it wrong-and-
explicit is what turns it into an auditable finding in §7 instead of an invisible one.

---

## 7. Post-import analysis: from code to action items

Once the estate is YAML behind a Pydantic schema, auditing is a query, not an API crawl.

`bin/github-estate-audit` (cyclopts) loads the same models, runs a rule set, and emits
findings as `(rule_id, axis, severity, repo, current, expected, remediation)`:

```
github-estate-audit run                       # markdown + JSON report
github-estate-audit run --axis security       # one axis
github-estate-audit run --emit-tasks          # create witan tasks under the project slug
github-estate-audit drift                     # live GitHub vs. declared YAML
```

Rules are pure functions over the resolved model, which makes them trivially testable with
fixtures and cheap to add — the rule set is meant to grow every time someone notices something.

### Axis: Security

| ID | Rule | Already known to fire |
|---|---|---|
| SEC-01 | Default branch has no ruleset or branch protection | `ol-django`, and likely most of the fleet |
| SEC-02 | Force-push to default branch not blocked | every sampled repo |
| SEC-03 | No required status checks on default branch | every sampled repo |
| SEC-04 | Secret scanning or push protection disabled | `hq` (private) |
| SEC-05 | Dependabot alerts or security updates disabled | every sampled repo + org default |
| SEC-06 | Outside collaborator holds `admin` or `maintain` | `mitxonline` has 1 direct collaborator |
| SEC-07 | Webhook without a secret, or a non-HTTPS URL | unknown |
| SEC-08 | Write-enabled deploy key on an active repo | unknown |
| SEC-09 | Actions secret not rotated in > 12 months | unknown |
| SEC-10 | Org allows members to change repo visibility / create public repos | **fires now** |
| SEC-11 | Installed app holds write scopes beyond its function | e.g. `jetify-cloud` and `sync-by-unito` hold `organization_hooks:write` |
| SEC-12 | Our own app's live permissions diverge from `docs/github-app-permissions.md` | — |
| SEC-13 | Private repo with weaker controls than the public baseline | `hq` |
| SEC-14 | No CODEOWNERS on a tier-1 repo | unknown |

### Axis: Consistency

| ID | Rule | Already known to fire |
|---|---|---|
| CON-01 | Merge strategy deviates from archetype | 4 of 5 sampled repos disagree |
| CON-02 | `delete_branch_on_merge` disabled | `hq` |
| CON-03 | Default branch is not `main` | unknown |
| CON-04 | Team grants deviate from archetype | unknown |
| CON-05 | No topics, or topics outside the controlled vocabulary | `mit-learn`, `ol-django`, `mitxonline` have none |
| CON-06 | Missing LICENSE / README / SECURITY.md | unknown |
| CON-07 | Archived repo still grants write to a team | unknown |
| CON-08 | Issue-label set deviates from the org standard | unknown |
| CON-09 | Repo has no `tier` custom property → **no org ruleset targets it, so it is unprotected** (§5.4) | all 317 today; must be zero after phase 3 |
| CON-10 | Empty description or homepage on an active repo | unknown |

### Axis: Developer experience

| ID | Rule | Already known to fire |
|---|---|---|
| DX-01 | `allow_auto_merge` disabled | every sampled repo |
| DX-02 | Ruleset requires a status check that no workflow produces (permanently blocked PRs) | — |
| DX-03 | Workflow exists that no ruleset requires (CI that cannot block a merge) | likely widespread |
| DX-04 | Required-check names drift from workflow job names after a rename | — |
| DX-05 | No Renovate/Dependabot config on an active repo | unknown |
| DX-06 | No PR or issue template, and no inheritance from the `.github` repo | unknown |
| DX-07 | Active repo with no push in 12+ months → archive candidate | unknown |
| DX-08 | Environments far exceeding the allowlist → CI leaking review apps | `mitxonline` (412), `mit-learn` (44) |
| DX-09 | Repo not granted to `odl-engineering` → invisible to the team | unknown |
| DX-10 | Zero org seat headroom (`filled_seats == seats`) → next hire cannot be added | **fires now** (39/39) |

DX-02 and DX-03 are the pair worth building first. Together they answer "is our CI actually
load-bearing?" and the sample says the answer is currently no — every protected branch requires
zero checks while every repo runs plenty.

### `drift` mode

Re-crawls live GitHub and diffs against declared YAML. This is what keeps the estate managed
after the import: any out-of-band console change surfaces as a diff. Run it nightly in Concourse
and route findings to the project's witan task list.

---

## 8. Phasing

| Phase | Work | Gate |
|---|---|---|
| **0** | Widen the GitHub App (§2). Write `docs/github-app-permissions.md`. Verify with a read-only crawl. | App can read every resource type in §3.3 |
| **1** | Build `bin/github-org-inventory`. Crawl. Human-confirm archetype per repo. Commit `data/`. | 317 repos classified; estate report reviewed |
| **2** | Author `organization/`. Import org, teams, members. Define the `tier` custom-property schema (§5.4) — now a prerequisite, not a fast-follow. | **Empty diff** on `ol-substructure-github-organization` |
| **3** | Author `repositories/`. Import in ~25-repo batches, including each repo's `tier` value. | **Empty diff** after every batch, and on the whole stack |
| **3.5** | Add the ~4 property-targeted org rulesets at `enforcement: evaluate`. Watch, then promote to `active`. | Rule-suite logs show the expected repos matching and no surprises |
| **4** | Build `bin/github-estate-audit`. Run all three axes. Emit witan tasks. | Backlog exists and is triaged |
| **5** | Remediate by tightening archetypes, not per-repo edits. Land in reviewed waves. | Each wave previews clean and is approved |
| **6** | Nightly `drift` job in Concourse; org custom-properties schema populated; consider Vault-sourced Actions secrets. | Drift job green |

Phases 0–3 change nothing on GitHub. Phase 5 is where behaviour changes, and it is entirely
downstream of a reviewed backlog — which is the point of doing the import first.

---

## 9. Decisions

All four are now settled — 1 by the capability probe (2026-07-31), 2 and 3 on 2026-08-03, and
4 as a deliberate deferral. The reasoning is kept in full rather than collapsed to the outcome,
because the evidence is what makes a decision re-examinable when the estate changes.

1. ~~**Org rulesets vs. per-repo rulesets.**~~ **RESOLVED 2026-07-31 — property-targeted org
   rulesets work.** See §5.4; the plan above already reflects the outcome. Kept here for the
   evidence trail.

   Two of the three sub-questions were settled by local inspection:

   - **Provider support: yes.** `pulumi_github` 6.x models it —
     `OrganizationRulesetConditionsRepositoryPropertyArgs(includes=[…], excludes=[…])`,
     each entry `{name, property_values, source}`. The `source` field distinguishes
     `custom` from `system` properties (`repository_visibility`, `language`, `fork`,
     `topic`), so the fork fleet may be targetable with no custom schema at all.
   - **Custom properties enabled for the org: yes.** `GET /orgs/mitodl/properties/schema`
     returns **HTTP 200** with `[]`. A plan-gated feature would 403/404; the schema is
     merely empty.
   - **Org rulesets on the Team plan: unknown.** `GET /orgs/mitodl/rulesets` 404s, but the
     token holds only `gist, project, read:org, repo, workflow`. GitHub returns 404 rather
     than 403 for unauthorized org-admin endpoints, so "unavailable on Team" and "token
     can't see it" are currently indistinguishable.

   **Probe results (2026-07-31, `bin/github-ruleset-capability-probe`):**

   | Check | Result |
   |---|---|
   | C1 org rulesets available on the **Team** plan | **PASS** — 0 existing |
   | C2/C3 custom property create + assign | PASS |
   | C4 org ruleset accepts `repository_property` | PASS |
   | C5 condition persists round-trip | PASS |
   | **C7 `evaluate` (dry-run) enforcement** | **PASS** — not Enterprise-gated |
   | C8 system-property targeting | **PASS** — the name is `visibility`, not `repository_visibility` |
   | C9 per-repo ruleset fallback | PASS |
   | **C6c property targeting actually matches** | **PASS** — `ol-infrastructure=True`, `ol-django=False`, agreeing across both endpoints |

   Four consequences, three of them good:

   - **Property targeting matches correctly** (C6c): with the ruleset briefly `active`,
     the labeled repo saw it and the unlabeled control did not, and both read endpoints
     agreed. This is what §5.4 is built on.
   - **`evaluate` mode is available.** The earlier worry that phase 5 would have to
     roll out blind is gone — a ruleset can be trialled fleet-wide in log-only mode
     before it blocks anything.
   - **System properties work**, so the ~60 upstream forks and any visibility-based
     rule can be targeted with no custom schema at all.
   - **A read-API gotcha that affects §7's `drift` mode.** Controls C6a and C6b proved
     that *no* read endpoint reports a non-enforcing ruleset as applying to a repo: a
     ruleset targeting `~ALL` in **evaluate** mode is invisible to both
     `/repos/{repo}/rulesets?includes_parents=true` **and**
     `/repos/{repo}/rules/branches/{branch}`. Only `active` rulesets appear. The drift
     detector must therefore enumerate rulesets from `/orgs/{org}/rulesets` and
     `/repos/{repo}/rulesets` directly; using the effective-rules endpoints would
     silently under-report every disabled or evaluate-mode ruleset in the estate.

   Re-run any time with `bin/github-ruleset-capability-probe` (needs
   `gh auth refresh -h github.com -s admin:org`); add `--allow-active` for C6c. Everything
   it creates is namespaced and torn down in a `finally`; verified clean after each run.

   **Methodological note worth keeping.** The first version of the probe created its
   ruleset with `enforcement: disabled` — the obviously safe choice — and reported C6 as a
   clean FAIL. That was a false negative manufactured by the safety choice: disabled
   rulesets are invisible to the endpoint doing the measuring. The tell was that the test
   *and* its control both came back negative. When a probe returns a uniform negative,
   suspect the measurement before the capability, and add a control that is true by
   construction (here, a ruleset targeting `~ALL`) to check the measurement can detect
   anything at all.
2. ~~**Do forks belong in Pulumi at all?**~~ **RESOLVED 2026-08-03 — import them, as the
   `fork` archetype with `tier: unmanaged`.**

   The deciding argument is the same one §4.4 already makes for archived repos, and it is
   about *absence being ambiguous* rather than about completeness. After phase 3, CON-09 reads
   "repo has no `tier` property, so no org ruleset targets it, so it is unprotected". A repo
   that is missing from Pulumi state entirely produces exactly the same observable signature as
   a repo someone forgot to label — an unprotected repo the audit cannot see. Importing the
   forks as `tier: unmanaged` converts "invisible to the audit" into "explicitly and
   deliberately untargeted", which is a claim a reviewer can disagree with.

   The cost is ~60 YAML files carrying two lines each (`archetype: fork` plus the name), and
   no ruleset resources at all, since §5.4 targets `tier` and nothing targets `unmanaged`.
   That is cheap enough that the stack-size argument for excluding them does not survive
   contact with the audit-noise argument for including them.

3. ~~**Seat pressure.**~~ **RESOLVED 2026-08-03 — keep `Membership` in Pulumi; the seat
   ceiling is an operational precondition for phase 2, not a design constraint.**

   Confirmed against the live org (`gh api /orgs/mitodl --jq .plan`):

   ```json
   {"name": "team", "seats": 39, "filled_seats": 39}
   ```

   `seats == filled_seats`, so there is genuinely zero headroom. But the framing in the
   original question was wrong in a way worth correcting: a `pulumi up` that fails on the 40th
   member is *loud, immediate, and harmless* — nothing is half-applied and nobody is locked
   out. That is not the failure mode to design around.

   The real cost is that onboarding moves onto the infra team's critical path, because
   `ol-substructure-github-organization` requires manual approval before apply (§2.2). That is
   a process consequence to accept knowingly, not a reason to leave 39 members unmanaged.

   Two things follow, neither of which blocks the design:

   - **Verify the billing behaviour before phase 2, not during it.** Whether adding a 40th
     member auto-provisions a paid seat or hard-fails depends on the billing arrangement, and
     that is not readable from the API — `/orgs/mitodl` reports the seat counts above but not
     the billing cadence. Ask whoever owns the GitHub bill. The answer changes the runbook,
     not the plan.
   - **`Membership` carries `protect=True`**, joining the org-level resources in §2.2.
     Deleting a `Membership` removes a human from the org; a bad merge to `members.py` should
     fail the apply rather than evict people. This is the counterweight that the original
     §2.2 list omitted, because it only considered repository deletion.

   New audit rule **DX-10**: zero seat headroom (`filled_seats == seats`) — the next hire
   cannot be added without a billing change. Fires now.

4. **Actions secrets into Vault** (§4.1) — real value, but a source-of-truth migration.
   Recommend deferring to its own project rather than smuggling it into this one. Note that
   §4.1's reasoning was rewritten on 2026-08-03: the resource is importable after all, and the
   reason to keep values out is the empty-diff gate, not a provider limitation.
