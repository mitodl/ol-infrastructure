# Who can reach what in `mitodl`, and how uniform it is

Companion to `docs/github-app-installation-audit.md` (machines) — this one covers
**people**: team grants across the repository fleet, and the direct user grants that sit
beside them.

Measured 2026-08-10, against the fleet as of PR #5324. Re-measure with:

```sh
uv run bin/github-estate-audit access      # needs credentials -- see "Why rosters are not committed"
uv run bin/github-estate-audit run --axis security
```

## The fact that makes all of this load-bearing

```
GET /orgs/mitodl  ->  "default_repository_permission": "none"
```

Org membership grants **no** baseline repository access. Every path to a repo is a team
grant, a direct collaborator grant, or org ownership (nine people hold implicit admin
everywhere).

This is worth stating first because it decides whether the rest of the document is
reporting anything. Under a `read` or `write` org default, "this repo has no team grants"
would be cosmetic — everyone would already have access. Under `none` it means what it
says.

## Team grants: 27 shapes for 176 repos

| | |
|---|---|
| Active repos | 176 |
| Distinct grant shapes | **27** |
| Repos with **no team grants at all** | **80 (45%)** |
| ...of those, with no direct grant either | **66** |

A "shape" is one distinct set of (team, permission) pairs. 176 repos drawn from four
shapes is a model; 176 drawn from 27 is an accretion of one-off decisions. The long tail is
almost entirely singletons — 15 of the 27 shapes apply to exactly one repo each.

The eight most common shapes cover 156 of 176 repos:

| Repos | Shape |
|---|---|
| 80 | *(no team grants)* |
| 35 | `arbisoft-contractors:push, odl-engineering:push` |
| 17 | `odl-engineering:push` |
| 6 | `odl-engineering:push, odl-engineering-owners:admin` |
| 6 | `odl-engineering-owners:admin` |
| 5 | `odl-engineering:maintain` |
| 3 | `devops:admin` |
| 2 | `devops:admin, odl-engineering:maintain` |

### The 66 unreachable repos

66 active repos have neither a team grant nor a direct grant. Only the nine org owners can
reach them: **50 forks, 15 libraries, and one `infrastructure` repo — `ol-concourse`**.

The forks are arguable — they are upstream mirrors, and nobody edits them day to day. The
15 libraries and `ol-concourse` are not: `ol-concourse` is named in `archetypes.yaml` as one
of the three `infrastructure` repos, and it grants nobody anything.

Reported as **CON-12** so this stays measured rather than rediscovered.

### Teams holding no grant, and why only two of them are a problem

Six of the 14 teams hold no grant on any active repo. Three of those are correct and
should stay that way:

- `copilot` (30 members) assigns Copilot seats.
- `vault-developer-access` (20) and `vault-devops-access` (6) gate Vault secrets and are
  declared `privacy: secret` in `organization/teams.py`.

They are listed in `audit.NON_ACCESS_TEAMS` so a future "team with no grant" rule does not
fire on them.

The other three are review-routing teams, and **one of them is silently broken**:

| Team | Named in CODEOWNERS of | Holds a grant there? |
|---|---|---|
| `owners-mit-open` | `open-discussions` | **yes** — `push`. Works. |
| `owners-mit-learn` | `mit-learn`, `learn-ai` | **no grant on any repo** |
| `code-owners-mitx-online` | *(none found)* | no |

**A team named in CODEOWNERS only receives review requests if it has access to the
repository.** Named-but-ungranted fails silently: no error, no warning, the request is
simply dropped. `mit-learn` and `learn-ai` both name `@mitodl/owners-mit-learn` and grant
it nothing, so its reviews have never been requested. `open-discussions` is the control
that proves the mechanism — same pattern, grant present, works.

### Permission levels are inconsistent per team

Three teams hold more than one level across the fleet, and `arbisoft-contractors` holds
three. That is variance with no evident rationale:

| Team | Active repos | Levels |
|---|---|---|
| `odl-engineering` | 81 | `push`:69, `maintain`:12 |
| `arbisoft-contractors` | 51 | `push`:44, `maintain`:4, `triage`:3 |
| `odl-engineering-owners` | 24 | `admin`:24 |
| `devops` | 12 | `admin`:12 |
| `ol-data` | 2 | `maintain`:1, `push`:1 |
| `devops-contractors` | 2 | `push`:2 |
| `code-owners` | 1 | `push`:1 |
| `owners-mit-open` | 1 | `push`:1 |

`odl-engineering-owners` is **not** redundant with org ownership, which is worth recording
because it looks like it should be. The team has 8 members and the org has 9 owners, but
they only overlap on 5 — `annagav`, `jkachel` and `odlbot` are in the team without being
org owners, so the grant is their only admin path.

## Direct user grants: 80 grants, 19 people, 5 kinds

SEC-06 counts 69 repos carrying direct grants. That number says how much there is to clean
up and nothing about how. Splitting by *what removal would actually do*:

| Kind | Count | What it means |
|---|---|---|
| `owner-implicit` | 37 | The holder is an **org owner**. Implicit admin on every repo survives the deletion, whatever the teams say. |
| `redundant` | 25 → 8 | Team access already meets or beats the direct grant. Delete freely. |
| `level-only` | 24 → 14 | Repo reach is kept; the **elevated rights** drop to the team's level — which is the SEC-15 target. |
| `no-access` | 23 → 13 | The direct grant is the **only** path in. Removing it revokes access. |
| `outside` | 8 | Not an org member at all. |

**59 of 80 can be removed with no new team work.** The other 13 gate the cleanup: each one
needs a team grant added first, which for most of them means fixing CON-12 on that repo.

The arrows are a correction, not a change in the estate. An earlier pass ranked every grant
by team membership alone, which ignored **org ownership as a third access path** — the one
the other two cannot revoke. That put 10 owner-held grants in `no-access`, the bucket whose
whole meaning is "removing this revokes access", and so overstated the gating set by nearly
half. Owners get their own bucket rather than being folded into `redundant` because implicit
admin survives roster churn and a redundancy verdict does not.

Note what `level-only` does and does not preserve: the person keeps their reach into the
repository, and **loses the elevated permission**. That is the intended outcome under the
SEC-15 policy, not a no-op.

Concentration is high — three people hold 44% of all direct grants:

| Person | Repos | Kinds |
|---|---|---|
| `rhysyngsun` | 20 (all admin) | 9 redundant, 11 load-bearing |
| `gumaerc` | 8 (all admin) | 4 redundant, 4 load-bearing |
| `odlbot` | 7 | 4 redundant, 3 load-bearing |

`odlbot` is an automation account, so its grants should be reviewed as service access
rather than as a person's.

### Four outside collaborators

Not org members, so no team can currently cover them:

| Person | Repos | Level |
|---|---|---|
| `indagation` | 3 | write |
| `NotoriousMKD` | 2 | write |
| `sfrucht` | 2 | write |
| `MaferMazu` | 1 | write |

All at `write`, none at admin. Each is a decision rather than a cleanup: invite to the org
and a team, or remove. Per plan §4.7 the target state is no direct grants at all, which
means outside collaborators need somewhere to go before that target is reachable.

## Why rosters are not committed

`bin/github-estate-audit access` needs credentials, unlike `run`. Classifying a direct
grant as redundant or load-bearing requires knowing who is in which team, and team
membership is deliberately **not** in `data/`.

`vault-developer-access` and `vault-devops-access` are declared `privacy: secret` so their
membership is not advertised inside the org — and `ol-infrastructure` is a **public**
repository. Committing rosters would publish precisely what that setting exists to
withhold. So `access` fetches them per run and never writes them to disk.

Worth flagging separately: `_direct_collaborators` **is** committed, and this repo is
public, so "person X holds admin on repo Y" is already published. That predates this audit
and the audit depends on it, but it deserves a decision of its own rather than inheritance
by default.

## A candidate uniform model

Not applied — this is the proposal the numbers point at.

**Default, from the archetype, for every non-fork active repo:**

```yaml
teams:
  odl-engineering: push          # broad engineering, capped at push (2026-08-05 policy)
  odl-engineering-owners: admin  # not redundant with org ownership -- see above
```

**Per-repo additions, only where there is a reason:**

- `devops: admin` on `infrastructure` (already the archetype since #5324).
- `arbisoft-contractors: push` where contractors are actually staffed — flattened from
  today's push/maintain/triage spread to one level.
- A product owners team (`owners-mit-learn`, `owners-mit-open`) at `push` on the repos
  whose CODEOWNERS name it. This is what fixes the silent-drop bug.

That collapses 27 shapes to roughly 5 and closes CON-12, at the cost of granting
`odl-engineering: push` on 80 repos that grant nothing today. **That is an access
widening**, which is a different risk class from SEC-15's narrowing and is why nothing here
is applied yet. The open questions are in the tracked tasks: what forks should get, and
whether "reachable only by an org owner" is deliberate on any of the 66.

## Sequencing

1. **Fix `owners-mit-learn`** on `mit-learn` and `learn-ai`. Two grants; fixes a silent
   failure. No downside.
2. **Delete the 59 free direct grants** (`redundant` + `level-only` + `owner-implicit`).
   Nobody loses their reach into a repository. The 14 `level-only` grants do lose their
   elevated permission, which is the SEC-15 target rather than a side effect.
3. **Decide the uniform model**, then apply it — which closes CON-12 and unblocks the 13
   `no-access` grants.
4. **Remove the remaining 13** once their repos have team grants.
5. **Decide on the 4 outside collaborators** — invite or remove.

Steps 1 and 2 are safe today. Step 3 is the one that needs a decision.
