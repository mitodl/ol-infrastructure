# Third-party GitHub App installations on `mitodl`

SEC-11 of the estate audit. Companion to `docs/github-app-permissions.md`, which covers our
own app; this one covers the 21 apps we did not write.

Measured 2026-08-07. Re-measure with the commands in [How this was measured](#how-this-was-measured).

## The short version

22 apps are installed (the audit was written against 21; `witan-agent-graph` was added
2026-08-03). **12 of them can see every repository in the org**, including all 140 archived
ones. Six hold `contents:write` or `workflows:write` org-wide, which is a path to executing
code in CI.

Three findings are actionable without asking anyone:

1. **`organization_hooks:write` is held by three installs and used by none.** The org has
   **zero** org-level webhooks. An org webhook is a firehose of every event in the org to an
   arbitrary URL, and it is the one permission on this list that no per-repo control limits.
2. **Four installs show no activity of any kind** across three independent signals, while
   holding write scopes: `jetify-cloud`, `google-labs-jules`, `netlify`, and
   `minware-data-ingest`.
3. **`codecov` is installed org-wide and used by 11 repos.** Narrowing it to those repos is
   a scope reduction with no behaviour change.

**The one thing this audit could not measure:** which repositories the ten `selected`
installs are actually scoped to. There is no REST endpoint that reports it to an org admin —
`GET /orgs/{org}/installations` omits it, and `GET /user/installations/{id}/repositories`
requires the app's own token. It has to be read from each install's settings page. Treat
"selected" below as "narrower than all, extent unknown".

## Permissions are vendor-side; scope is ours

An installed app's permission set is declared by the app's author. We cannot reduce
`gitguardian`'s `organization_hooks:write` while keeping GitGuardian. The levers we do have:

- **Uninstall.** The only way to remove a permission.
- **Narrow `repository_selection`** from all repos to a named set.
- **Decline the update.** Permission changes require an org owner to approve, so a vendor
  widening its scope is a decision point, not a fait accompli — provided someone reads it.

Pulumi models `AppInstallationRepository` (which repos an install can see) but not the
permission set. **SEC-11 therefore stays an audit rule plus a periodic human review, not an
IaC-enforced control.** Repo scoping *is* enforceable in code if we decide to pin any of it.

## Inventory

Ordered by blast radius: org-wide write first, then org-wide read, then scoped.
"Activity" is the strongest of the three signals in
[How this was measured](#how-this-was-measured); `-` means all three were zero.

| App | Repos | Notable scopes | Activity (180d) | Assessment |
|---|---|---|---|---|
| `ol-infrastructure-as-code` | all | `organization_administration:write`, `administration:write`, `secrets:write`, `organization_hooks:write` | in use (Pulumi) | **Ours.** Held to `docs/github-app-permissions.md` — SEC-12, not this audit. |
| `gitguardian` | all | **`organization_hooks:write`**, `issues:write`, `members:read` | 57 comments | **In use.** Secret-scanning comments on PRs. `organization_hooks:write` is unexplained — see finding 1. |
| `sync-by-unito` | all | `organization_projects:write`, `repository_projects:write`, `repository_hooks:write`, `issues:write` | 3,598 comments, 1,634 issues | **In use, heavily.** Broadest legitimate writer in the org. |
| `slack` | all | `contents:write`, `workflows:write`, `deployments:write`, `actions:write` | none | **Question.** Subscribed to 25 event types, so it is receiving; but a notification integration writing contents and workflows is broader than the function. |
| `claude` | all | `contents:write`, `workflows:write`, `actions:write`, `repository_hooks:write` | 13 comments | **In use, lightly.** Candidate for narrowing to the repos that actually use it. |
| `renovate` | all | `contents:write`, `workflows:write`, `administration:read` | 1,702 PRs, 183 comments | **In use, heavily.** Org-wide is correct for its function. |
| `renovate-approve` | all | `contents:write`, `pull_requests:write` | 1,057 comments | **In use.** Minimal scope. |
| `sentry` | all | `contents:write`, `actions:write`, `repository_hooks:write`, `administration:read` | 1,006 comments, 27 PRs | **In use.** |
| `codecov` | all | `administration:read`, `checks:write` | 5 comments | **Narrow it.** 11 repos carry `codecov.yml`; 10 workflows call `codecov-action`. Org-wide scope buys nothing. |
| `haticahq` | all | very broad **read**: `secret_scanning_alerts`, `security_events`, `members`, `organization_events`, `team_discussions`, `vulnerability_alerts` — plus `repository_hooks:write` | none | **Confirm the contract is live.** Reads every secret-scanning and security alert in the org. Subscribed to zero events. |
| `minware-data-ingest` | all | all-read: `contents`, `deployments`, `environments`, `members`, `organization_projects` | none | **Confirm the contract is live.** Read-only, but org-wide. |
| `witan-agent-graph` | all | `contents:read`, `metadata:read` | (indexing) | **Fine.** Smallest scope of any org-wide install. |
| `pre-commit-ci` | selected | `contents:write`, `workflows:write` | 140 PRs; posts `pre-commit.ci - push` on every repo sampled | **In use, heavily.** |
| `render` | selected | `environments:write`, `workflows:write`, `actions:write`, `repository_hooks:write`, `deployments:write` | none measurable | **Question.** No `render.yaml` in the org, but Render is usually configured in its own UI, so absence is not proof. Do we run anything on Render? |
| `digitalocean` | selected | `repository_hooks:write`, `security_events:read`, `vulnerability_alerts:read` | none measurable | **Question.** No `.do/app.yaml` in the org. Same caveat as Render. |
| `hightouch-connect` | selected | `contents:write` | none measurable | **Probably in use** — 12 code references to Hightouch, which is in the data stack. Minimal scope. |
| `concourse-github-issue-pulumi` | selected | `contents:write`, `issues:write` | 361 issues all-time, **0 in 180d** | **Check whether it is retired.** |
| `netlify` | selected | `contents:read`, `checks:write`, `statuses:write` | none | **Remove if no sites remain.** No `netlify.toml` in the org. Lowest-risk of the dormant set. |
| `jetify-cloud` | selected | **`organization_hooks:write`**, `contents:write`, `deployments:write`, `repository_hooks:write` | none | **Remove.** No `devbox.json` anywhere in the org. Holds org-hook write with nothing to show for it. |
| `google-labs-jules` | selected | `contents:write`, `workflows:write`, `actions:write`, `administration:read` | 1 PR all-time, 0 in 180d | **Remove.** Installed 2025-08-08, subscribed to 34 event types, produced one PR. |
| `billing-vantage-sh` | selected | `organization_administration:read` | n/a | **Fine.** Read-only billing. |
| `ol-release-bot` | selected | `contents:write`, `deployments:write` | 4 PRs | **Ours, in use.** |

## Findings

### 1. `organization_hooks:write` is granted three times and used zero times

`gitguardian`, `jetify-cloud`, and our own `ol-infrastructure-as-code` can create and modify
**organization-level webhooks**. `GET /orgs/mitodl/hooks` returns an empty list: none exist.

This matters more than a repo-scoped write. An org webhook delivers every event in the
organization — pushes, membership changes, secret-scanning alerts, team changes — to a URL of
the holder's choosing, and no per-repository control constrains it. It is the cleanest
exfiltration path in the permission model, and it is currently granted to a vendor we cannot
audit and to a dormant install.

`jetify-cloud` is removable outright (finding 2). `gitguardian` is in active use, so the
choice is narrower: accept the grant with the fact recorded here, or narrow the install's
repository scope — which does *not* constrain `organization_hooks:write`, since org hooks are
not per-repo. Accepting it knowingly is a legitimate answer; not knowing was not.

Our own app's grant is SEC-12's business, and `docs/github-app-permissions.md` should justify
it or drop it.

### 2. Four installs are dormant and hold write scopes

No authored PRs or issues, no comments, and no code in the org referencing the vendor:

| App | Installed | Evidence of disuse | Write scopes still held |
|---|---|---|---|
| `jetify-cloud` | 2024-07-18 | no `devbox.json` in the org | `organization_hooks`, `contents`, `deployments`, `repository_hooks` |
| `google-labs-jules` | 2025-08-08 | 1 PR ever, none in 180d | `contents`, `workflows`, `actions` |
| `netlify` | 2019-12-11 | no `netlify.toml` in the org | `checks`, `statuses` (read-only on contents) |
| `minware-data-ingest` | 2024-03-02 | none | read-only |

`jetify-cloud` and `google-labs-jules` are the two worth acting on: both hold scopes that let
them change what runs in CI, and `google-labs-jules` in particular is subscribed to 34 event
types for a single PR's worth of output.

### 3. Org-wide scope is the default, not the exception

12 of 22 installs see all 316 repos, including 140 archived ones. Archiving makes a repo
read-only to humans; it does not narrow what an installed app can read from it. Every archived
repo's contents, secrets metadata and security alerts remain within reach of every org-wide
install.

`codecov` is the clearest case for narrowing — 11 repos use it, and its scope is org-wide for
no reason. `claude` is the second: `contents:write` plus `workflows:write` across the fleet,
against light measured use.

### 4. The audit has a blind spot the API cannot fill

Per-install repository scoping is not exposed to org-admin credentials. Any automated SEC-11
check can therefore report *whether* an install is narrowed, but not *to what* — so a `selected`
install silently widened to 200 repos looks identical to one pinned at 2. Closing this needs
either the settings UI or `AppInstallationRepository` under Pulumi, at which point the declared
set becomes the check.

## How this was measured

Permissions, scope and age:

```sh
gh api /orgs/mitodl/installations --paginate --jq \
  '.installations[] | [.app_slug, .repository_selection, (.created_at|split("T")[0]),
   (.suspended_at // "-"), ((.events // [])|length),
   (.permissions|to_entries|map(.key+":"+.value)|sort|join(","))] | @tsv'
```

Usage is measured three ways, because **no single signal is evidence of disuse**. A
check-posting app (`gitguardian`, `codecov`) or a deploy-status app (`render`, `netlify`)
legitimately never authors a PR, so an authorship count alone would mark half this list
dead. An app is called dormant here only when all three come back empty *and* nothing in
the org's code references the vendor:

```sh
# 1. authored PRs and issues
gh api -X GET search/issues --raw-field q="org:mitodl author:<slug>[bot]" --jq .total_count
# 2. comments
gh api -X GET search/issues --raw-field q="org:mitodl commenter:<slug>[bot] updated:>=<date>" --jq .total_count
# 3. check runs, statuses and deployments on a recent commit
gh api "repos/mitodl/<repo>/commits/<sha>/check-runs" --jq '[.check_runs[].app.slug]|unique|.[]'
gh api "repos/mitodl/<repo>/commits/<sha>/status"     --jq '[.statuses[]?.context]|unique|.[]'
```

Config-file presence (`netlify.toml`, `render.yaml`, `codecov.yml`, `devbox.json`) is a
fourth signal, and the weakest: Render, Netlify and DigitalOcean App Platform can all be
configured entirely in the vendor's UI, so an absent config file is suggestive, never
conclusive. It is used above only to *support* a dormancy call the other three already made.

Org webhooks — the check behind finding 1 — need `admin:org_hook`, which a plain `gh` token
does not carry. The App installation token does:

```sh
uv run bin/github-org-inventory report --refresh   # then read .org_webhooks from the cache
```

## Review cadence

Permissions change vendor-side without a commit landing here, so this document is a snapshot,
not a control. Re-run the enumeration when an org owner is asked to approve a permission
change, and on a schedule — the phase-6 nightly drift job (§8 of
`docs/plans/github-org-pulumi-import.md`) is the natural home for it.
