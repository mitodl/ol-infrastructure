# GitHub App installations on `mitodl`

SEC-11 of the estate audit. Companion to `docs/github-app-permissions.md`, which covers
`ol-infrastructure-as-code` in detail; this one covers all 22 installations. Two of them are
ours — `ol-infrastructure-as-code` and `ol-release-bot` — and are marked as such below.

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
2. **Four installs show no activity of any kind** across three independent signals. Three of
   them hold write scopes — `jetify-cloud`, `google-labs-jules` and `netlify`; the fourth,
   `minware-data-ingest`, is read-only but org-wide.
3. **`codecov` is installed org-wide and referenced by at least 13 repos.** Narrowing it to
   that set is the obvious scope reduction, but the evidence here cannot establish it is
   behaviour-neutral — see the caveat in the inventory row.

**The one thing this audit could not measure:** which repositories the ten `selected`
installs are actually scoped to. There is no REST endpoint that reports it to an org admin —
`GET /orgs/{org}/installations` omits it, and `GET /user/installations/{id}/repositories`
requires the app's own token. It has to be read from each install's settings page. Treat
"selected" below as "narrower than all, extent unknown".

## Decisions (2026-08-18)

None of these are Pulumi-manageable — every action below is a manual step in
Settings -> GitHub Apps, same as SEC-10's `members_can_change_repo_visibility`. This
section is the source of truth for what was decided; the inventory table below is left
as originally measured rather than edited in place, so the evidence trail stays intact.

**Already resolved, no action needed:** `google-labs-jules` and
`concourse-github-issue-pulumi` are no longer installed — re-checked 2026-08-18 against
`GET /orgs/mitodl/installations`, neither appears. Whoever removed them did so outside
this audit.

**Remove** (dormant, no owner objection):
- `jetify-cloud` — confirmed dormant a second time 2026-08-18 (still no `devbox.json`
  anywhere in the org). Removing this also closes finding 1's `organization_hooks:write`
  count from three grantees to two.
- `render` — no repo runs anything on Render.
- `digitalocean` — no repo runs anything on DigitalOcean App Platform.
- `minware-data-ingest` — contract confirmed not live.
- `haticahq` — contract confirmed not live. This is the higher-value removal of the
  four: it was reading every secret-scanning alert and security event in the org
  (finding 3) for a dead contract.

**Keep:**
- `netlify` — still hosting at least one site, despite no `netlify.toml` in the org
  (config lives outside the repo, e.g. Netlify's own UI or a build hook).
- `gitguardian`'s `organization_hooks:write` (finding 1) — accepted knowingly rather
  than narrowed. GitGuardian is load-bearing and repository scope does not constrain
  this permission anyway (org hooks are not per-repo), so narrowing its repo selection
  would not have reduced this specific grant. Recorded here rather than left as an
  open question.

**Deferred, not blocking:**
- `codecov` and `claude` narrowing (finding 3) — both stay org-wide for now. `codecov`'s
  target set (13 repos, see the inventory row's caveat) is known; `claude`'s is not,
  since Anthropic's own install page is the only place that shows it (finding 4's blind
  spot applies to our own app installs too, not just third-party ones). Revisit either
  independently of this SEC-11 pass; narrowing is a pure scope reduction with no
  behavior change whenever it happens.
- `slack`'s `contents:write` + `workflows:write` (broader than a notification
  integration needs) — flagged for follow-up with whoever manages the Slack
  integration, not acted on now. Unlike the four removals above, nobody has confirmed
  whether the Slack GitHub app even supports narrowing, so this needs a person to check
  before it becomes an action item rather than a question.

## Permissions are vendor-side; scope is ours

An installed app's permission set is declared by the app's author. We cannot reduce
`gitguardian`'s `organization_hooks:write` while keeping GitGuardian. The levers we do have:

- **Uninstall.** The only way to remove a permission.
- **Narrow `repository_selection`** from all repos to a named set.
- **Decline the update.** Permission changes require an org owner to approve, so a vendor
  widening its scope is a decision point, not a fait accompli — provided someone reads it.

Pulumi models which repos an install can see, but not the permission set — and the resources
that model it **cannot be used with the provider this project has.** Both
`github.AppInstallationRepository` (one association) and
`github.AppInstallationRepositories` (the complete selected set) carry the same note in the
SDK:

> **Note**: This resource is not compatible with the GitHub App Installation authentication
> method.

`lib/github_helper.py:38-46` configures the provider with exactly that method
(`ProviderAppAuthArgs`), so pinning repo scoping in code needs a **second provider instance
authenticated with a PAT** — a new credential to store, rotate and justify, for a control
that governs 10 installations. That is a real decision, not a formality, and it is why the
recommendation below is a periodic review rather than "just declare it".

If it is ever taken on, use the **plural** resource: the singular one manages a single
app-to-repo association, so a set of 10 declared singly leaves an eleventh repo added by hand
invisible. The plural resource carries its own trap — GitHub cannot install an app with zero
repositories selected, so deleting it leaves one repository still attached rather than
detaching cleanly.

**SEC-11 therefore stays an audit plus a periodic human review, not an IaC-enforced control.**

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
| `codecov` | all | `administration:read`, `checks:write` | 5 comments | **Narrow it, after confirming the set.** 13 repos reference Codecov in CI (11 carry `codecov.yml`, 10 call `codecov-action`, and the two sets are not nested). That is a floor, not the set: `codecov.yml` is optional and Codecov also accepts CLI uploads from CI it does not integrate with, neither of which this search sees. Confirm against Codecov's own repo list before narrowing. |
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

### 2. Four installs are dormant; three of them hold write scopes

No authored PRs or issues, no comments, and no code in the org referencing the vendor:

| App | Installed | Evidence of disuse | Write scopes still held |
|---|---|---|---|
| `jetify-cloud` | 2024-07-18 | no `devbox.json` in the org | `organization_hooks`, `contents`, `deployments`, `repository_hooks` |
| `google-labs-jules` | 2025-08-08 | 1 PR ever, none in 180d | `contents`, `workflows`, `actions` |
| `netlify` | 2019-12-11 | no `netlify.toml` in the org | `checks`, `statuses`, `pull_requests` — but `contents` is read-only |
| `minware-data-ingest` | 2024-03-02 | none | **none — read-only throughout** |

`jetify-cloud` and `google-labs-jules` are the two worth acting on: both hold scopes that let
them change what runs in CI, and `google-labs-jules` in particular is subscribed to 34 event
types for a single PR's worth of output. `minware-data-ingest` is a lower-priority case — the
question there is whether the contract is live, not what it could do with the access.

### 3. Org-wide scope is the default, not the exception

12 of 22 installs see all 316 repos, including 140 archived ones. Archiving makes a repo
read-only to humans; it does not narrow what an installed app can read from it: an archived
repo stays within reach of every org-wide install, at whatever that install's permissions
allow. **Repository scope and permission are independent** — org-wide reach does not confer
`contents` or `security_events` on an app that was never granted them, which is why the
permission column above matters as much as the scope column. The install to reason about is
the one holding both, and `haticahq` is the example: org-wide *and* granted
`secret_scanning_alerts:read`, `security_events:read` and `contents:read`, so it can read every
security alert in the org including those on repos nobody has touched in years.

`codecov` is the clearest case for narrowing — org-wide scope against ~13 repos that reference
it — subject to confirming the real set first. `claude` is the second: `contents:write` plus
`workflows:write` across the fleet, against light measured use.

### 4. The audit has a blind spot the API cannot fill

Per-install repository scoping is not exposed to org-admin credentials. Any automated SEC-11
check can therefore report *whether* an install is narrowed, but not *to what* — so a `selected`
install silently widened to 200 repos looks identical to one pinned at 2.

The settings UI closes it for a human. Closing it in code means
`github.AppInstallationRepositories`, which — as noted above — **cannot run on this project's
provider**, because App-installation authentication is unsupported for that resource. It would
need a second, PAT-authenticated provider. Weigh that against what it buys before assuming it
is the obvious fix.

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

Config-file and vendor-name presence in the org's code is a fourth signal, and the weakest:

```sh
# per-vendor config files -- the `-` counts are what support a dormancy call
for q in 'filename:netlify.toml' 'filename:render.yaml' 'filename:codecov.yml' \
         'path:.do/ filename:app.yaml' 'filename:devbox.json' 'hightouch' 'unito'; do
  printf '%-30s ' "$q"
  gh api -X GET search/code --raw-field q="org:mitodl $q" --jq .total_count
  sleep 3            # search is capped at 30 requests/minute, separate from the 5000/hr core bucket
done
```

**Count repositories, not files.** `total_count` counts matching *files*, so a repo with two
`codecov.yml` files would inflate it. Every per-repo number quoted above is deduplicated:

```sh
gh api -X GET search/code --raw-field q='org:mitodl filename:codecov.yml' \
  --jq '[.items[].repository.full_name] | unique | length'          # 11 repos
gh api -X GET search/code --raw-field q='org:mitodl codecov-action path:.github/workflows' \
  --jq '[.items[].repository.full_name] | unique | length'          # 10 repos
gh api -X GET search/code --raw-field q='org:mitodl codecov path:.github/workflows' \
  --jq '[.items[].repository.full_name] | unique | length'          # 13 repos -- the union
```

The two Codecov signals are **not nested**: `ol-data-platform` and `ol-django` call
`codecov-action` with no `codecov.yml`, while `mitxonline`, `ocw-studio` and
`mitx-grading-library` have the config and no action. Quoting either number alone understates
the set, which is why the inventory quotes the union and calls it a floor.

Three limits make this signal weak, and all three push the count **down**:

- Code search covers **default branches of non-archived repos only**.
- A vendor configured entirely in its own UI leaves no file. Render, Netlify and DigitalOcean
  App Platform all work this way.
- A tool invoked by a path this search does not know about is invisible — Codecov's CLI
  uploader needs neither `codecov.yml` nor `codecov-action`.

So an absent config file is suggestive, never conclusive. It is used above only to *support* a
dormancy call the other three signals already made, and never to establish a complete set.

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
