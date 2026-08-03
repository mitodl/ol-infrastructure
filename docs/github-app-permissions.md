# GitHub App permissions: `ol-infrastructure-as-code`

Authoritative record of the permissions granted to the GitHub App that Pulumi uses to manage
the `mitodl` organization, and the reason for each one.

- **App installation id:** `150389158` (org `mitodl`)
- **Wired through:** `ol_infrastructure.lib.github_helper.setup_github_provider()`
- **Credentials:** `src/bridge/secrets/pulumi/github_app.yaml` (sops)
- **Consumed by:** `docs/plans/github-org-pulumi-import.md`

This file is not documentation-after-the-fact — audit rule **SEC-12** diffs the live
installation's permissions against the tables below, so it has to stay accurate. Live state:

```bash
gh api /orgs/mitodl/installations \
  --jq '.installations[] | select(.app_slug=="ol-infrastructure-as-code") | .permissions'
```

## Current state vs. target

**2026-08-03 — the grant landed.** The installation went from `metadata:read,
repository_hooks:write` (scoped for its single original job, the OCW Studio webhook in
`src/ol_infrastructure/applications/ocw_studio/__main__.py`) to **29 of the 31 permissions
below**, `repository_selection: all`.

Three deltas remain between the live installation and this file. Until they are closed, SEC-12
fires on our own app:

| Permission | This file says | Live | Action |
|---|---|---|---|
| `dependabot_secrets` | write | **absent** | Grant. `DependabotSecret` cannot be managed without it. |
| `vulnerability_alerts` | read | **write** | Reduce to read. Over-grant, and it confounds the `RepositoryDependabotSecurityUpdates` test — see "Unresolved". |
| `organization_custom_roles` | ~~write~~ | absent | **Do not grant — withdrawn.** Custom *repository* roles are Enterprise-only; see below. |

Neither delta blocks phases 1–3: the import does not touch Dependabot secrets, and
`dependabot_secrets` is tier B, needed only before that resource type is managed.

### `organization_custom_roles` is withdrawn — Enterprise-only

The read-only gate (below) called `GET /orgs/mitodl/custom-repository-roles` and got:

```
404  {"message": "Feature not available for the mitodl organization."}
```

Not a permissions error — a **plan-tier** error. `mitodl` is on the Team plan (confirmed:
`/orgs/mitodl` reports `plan.name: "team"`), and custom repository roles are an Enterprise
feature. So `github.OrganizationCustomRole` and `github.OrganizationRepositoryRole` can never
work here, and the permission that unlocks them has nothing to unlock. Removed from the
required tables and added to "Deliberately excluded".

Note this is **not** the same as `organization_custom_org_roles` ("Custom organization roles"),
which *is* granted and *does* work — `GET /orgs/mitodl/organization-roles` returns 200. The two
UI labels differ by one word and gate entirely different features. Easy to conflate; this file
did, until the gate distinguished them.

### Gate result — 2026-08-03

A read-only crawl exercised every resource type in plan §3.3 using a minted installation token
(`scripts/github/` conventions; see the task record for the script). **31 of 33 read paths
returned 200/204.** The two that did not:

| Path | Result | Meaning |
|---|---|---|
| `/repos/mitodl/ol-infrastructure/dependabot/secrets` | 403 *Resource not accessible by integration* | the missing `dependabot_secrets` grant, exactly as predicted |
| `/orgs/mitodl/custom-repository-roles` | 404 *Feature not available* | Enterprise-gated, see above |

Everything the import actually depends on — repositories, topics, branches, protection,
rulesets, collaborators, webhooks, deploy keys, environments, labels, secret *names*, variables,
custom property values, autolinks, org settings, the property schema, org rulesets, teams,
members, org webhooks/secrets/variables, runner groups, org roles, blocks, the full repo listing,
PATs and installations — reads clean.

Tier column: **A** = required to read state during import · **B** = required to manage the
resource · **C** = read-only, exists so the §7 estate audit works without a separate PAT.

## Repository permissions

| UI label | API slug | Level | Tier | Why |
|---|---|---|:--:|---|
| Metadata | `metadata` | read | A | Mandatory for every app. Repo enumeration. |
| Administration | `administration` | write | B | The load-bearing one. `Repository`, `RepositoryTopics`, `BranchDefault`, `BranchProtection`, `RepositoryRuleset`, `RepositoryDeployKey`, `RepositoryAutolinkReference`, `RepositoryCollaborator(s)`, `TeamRepository`, and the `vulnerability-alerts` / `automated-security-fixes` toggles. |
| Contents | `contents` | write | B | `RepositoryFile`, `Branch`, `Release`. Needed to push CODEOWNERS / SECURITY.md fleet-wide in phase 5. |
| Workflows | `workflows` | write | B | Required for any `RepositoryFile` under `.github/workflows/`. **Write-only — GitHub defines no read level for this permission.** |
| Secrets | `secrets` | write | B | `ActionsSecret` (repo scope). Values are write-only and cannot be imported — see plan §4.1. |
| Variables | `actions_variables` | write | B | `ActionsVariable`. Slug **confirmed 2026-08-03** by reading back the live grant. |
| Dependabot secrets | `dependabot_secrets` | write | B | `DependabotSecret`. |
| Environments | `environments` | write | B | `RepositoryEnvironment`, for the curated allowlist only (plan §4.3). |
| Issues | `issues` | write | B | `IssueLabel`, `IssueLabels`, `RepositoryMilestone`. Label standardization (CON-08). |
| Pages | `pages` | write | B | `RepositoryPages`. Optional — drop if we never manage Pages. |
| Webhooks | `repository_hooks` | write | B | `RepositoryWebhook`. **Already held.** |
| Custom properties | `repository_custom_properties` | read | C | Reads a repo's own property values. Note: *setting* values is an org-level permission — see below. |
| Dependabot alerts | `vulnerability_alerts` | read | C | Audit SEC-05: which repos have open Dependabot alerts. Distinct from the on/off toggle, which is `administration`. |
| Secret scanning alerts | `secret_scanning_alerts` | read | C | Audit SEC-04. |
| Code scanning alerts | `security_events` | read | C | Audit: code-scanning alert state. |
| Deployments | `deployments` | read | C | Audit DX-08: which environments are live vs. abandoned. |
| Pull requests | `pull_requests` | read | C | Audit: merge-queue and review-policy checks. |

## Organization permissions

| UI label | API slug | Level | Tier | Why |
|---|---|---|:--:|---|
| Administration | `organization_administration` | write | B | `OrganizationSettings` **and `OrganizationRuleset`** — org rulesets live under org Administration, not a rulesets-specific permission. |
| Members | `members` | write | B | `Membership`, `Team`, `TeamSettings`, `TeamMembership`, `TeamMembers`, and the org half of `TeamRepository`. |
| Custom properties | `organization_custom_properties` | admin | B | `OrganizationCustomProperties` (the schema) **and `RepositoryCustomProperty`** (setting values on repos). `admin` is required to define the schema; `write` only sets values. This is the backbone of the §5.4 ruleset design. |
| Webhooks | `organization_hooks` | write | B | `OrganizationWebhook`. |
| Secrets | `organization_secrets` | write | B | Org-level Actions and Dependabot secrets. |
| Variables | `organization_actions_variables` | write | B | Org-level Actions variables. Slug **confirmed 2026-08-03**. |
| Self-hosted runners | `organization_self_hosted_runners` | write | B | `ActionsRunnerGroup`. |
| ~~Custom repository roles~~ | ~~`organization_custom_roles`~~ | — | — | **Withdrawn 2026-08-03 — Enterprise-only feature, unavailable on the Team plan.** See above. |
| Custom organization roles | `organization_custom_org_roles` | write | B | `OrganizationRole`, `OrganizationRoleTeam`, `OrganizationRoleUser`. |
| Blocking users | `organization_user_blocking` | write | B | `OrganizationBlock`. |
| Plan | `organization_plan` | read | C | Audit: seat pressure. Currently **39/39 filled** — adding a member via Pulumi will simply fail once full. **Read-only; GitHub defines no write level.** |
| Personal access tokens | `organization_personal_access_tokens` | read | C | Audit: fine-grained PATs holding org access. |
| Personal access token requests | `organization_personal_access_token_requests` | read | C | Audit: pending PAT requests. |
| Events | `organization_events` | read | C | Audit: correlate detected drift with who changed what. |

## Deliberately excluded

Each of these is a distinct blast-radius surface with no current consumer. Add only when a
concrete resource needs it:

`organization_copilot_seat_management`, `organization_copilot_agent_settings`, `codespaces`,
`organization_custom_roles` (Enterprise-only — see above; distinct from
`organization_custom_org_roles`, which is granted),
`organization_packages`, `packages`, `team_discussions`, `organization_projects`,
`repository_projects`, `organization_announcement_banners`, `interaction_limits`,
`enterprise_custom_properties_for_organizations`, and every user-scoped permission
(`email_addresses`, `followers`, `gpg_keys`, `git_ssh_keys`, `profile`, `starring`).

## Safety counterweights

`administration:write` permits repository **deletion**, and `organization_administration:write`
permits org-wide settings changes. Three mitigations, all enforced in code rather than by
convention:

1. Every `github.Repository` carries `pulumi.ResourceOptions(retain_on_delete=True)`. Removing
   a repo from the YAML data removes it from Pulumi state and **never** from GitHub.
2. Org-level resources (`OrganizationSettings`, `OrganizationRuleset`, `OrganizationWebhook`,
   `OrganizationCustomProperties`) carry `protect=True`, **and so does `Membership`** —
   `members:write` permits removing people from the org, which is as irreversible as deleting
   a repo. Added 2026-08-03; the first version of this list only considered repositories.
3. The Concourse job for `ol-substructure-github-organization` requires manual approval before
   `pulumi up`. `ol-substructure-github-repositories` may auto-apply once its empty-diff gate
   is green.

A fourth, from the §5.4 ruleset design: because org rulesets target a `tier` custom property,
an unlabeled repo matches *nothing* rather than everything. The failure mode of a mistake in
the labeling data is under-enforcement, which the audit catches (CON-09), not a fleet-wide
lockout.

## How each entry was verified

Worth recording, because a plausible-looking permission table is exactly the kind of artifact
that gets trusted without being checked. Three independent sources were used on 2026-07-31:

1. **GitHub's own OpenAPI description** — `components.schemas.app-permissions.properties`
   from `github/rest-api-description` (13 MB, fetched directly). This is authoritative for
   *slug spelling* and *allowed levels*, and is where `workflows` being write-only and
   `organization_plan` being read-only come from.
2. **The 21 live third-party installations in `mitodl`** —
   `gh api /orgs/mitodl/installations --jq '.installations[].permissions|keys[]'` returns real
   slugs as GitHub serializes them. This empirically confirmed `organization_administration`,
   `organization_hooks`, `vulnerability_alerts`, `environments`, `secret_scanning_alerts`.
3. **The REST "permissions required for GitHub Apps" doc**, for the endpoint → permission
   mapping the other two sources do not carry.

Three corrections this process produced, all of which would have caused a failed grant:

| Wrong | Right | How it was caught |
|---|---|---|
| `custom_properties` | `repository_custom_properties` / `organization_custom_properties` | Not in the OpenAPI vocabulary |
| ~~`actions_variables`, `organization_actions_variables`~~ | **Not a correction — the original guess was right.** See "The OpenAPI schema is not exhaustive" below. | *(withdrawn 2026-08-03)* |
| `vulnerability_alerts:write` for the alerts *toggle* | `administration:write` (the `vulnerability_alerts` permission covers reading alerts) | Endpoint doc lists it under Repository "Administration" |

**Beware the docs UI labels.** Org-level permissions display as "Webhooks", "Blocking users",
"Administration" but serialize as `organization_hooks`, `organization_user_blocking`,
`organization_administration`. Reading a heading and writing it down as the slug produces a
table that looks right and fails on contact. Both columns are given above for this reason: the
UI label is what you click when granting, the API slug is what SEC-12 compares.

## The OpenAPI schema is not exhaustive

Worth its own heading, because it caused this file to record a correction that was itself wrong.

The Variables slug was listed above as unconfirmed, and the correction table claimed
`actions_variables` / `organization_actions_variables` were *not real slugs*, on the evidence
that neither key appears in `components.schemas.app-permissions.properties` in GitHub's own
OpenAPI description. When the permission was actually granted on 2026-08-03, the installation
reported exactly those two keys:

```json
{"actions_variables": "write", "organization_actions_variables": "write"}
```

The original guess was right and the "correction" was wrong. **Absence from the
`app-permissions` OpenAPI vocabulary does not mean a permission does not exist** — that schema
lags the product. It remains authoritative for the spelling and allowed levels of slugs it
*does* carry (that is how `workflows` being write-only and `organization_plan` being read-only
were established, and both held up), but it cannot be used as proof of non-existence.

The general shape of the error: a negative result from an incomplete reference was treated as
a positive finding. Only reading back a live grant can settle whether a slug exists, which is
why step 3 of phase 0 is a read-back and not a doc review.

## Unresolved

**`RepositoryDependabotSecurityUpdates`.** The `automated-security-fixes` endpoint is grouped
with the `vulnerability-alerts` toggle, so `administration:write` is expected to cover it, but
this was inferred from adjacency rather than read off a doc heading. Confirm on first use; the
symptom of being wrong is a 403 on that resource alone.

Note that this test is currently **confounded**: the live installation holds
`vulnerability_alerts: write` (an over-grant relative to the `read` this file specifies), so if
the resource works we cannot tell which permission carried it. Reduce that entry to `read`
before running the test, or the result proves nothing.
