# 0011. Executable Offboarding Script Over a Prose Runbook

**Status:** Proposed
**Date:** 2026-08-04
**Deciders:** Infrastructure team
**Technical Story:** [#5235 — Write an offboarding runbook for employees](https://github.com/mitodl/ol-infrastructure/issues/5235)

## Context

### Current Situation

Issue #5235 asks for a prose offboarding runbook covering "removing people from" roughly a dozen
systems: Keycloak/SSO, AWS IAM, Kubernetes, GitHub, CI/CD, Vault, observability and
incident-response tools, DNS/CDN/control planes, and privileged SaaS.

Auditing the repository for where human access actually lives produced a far more concentrated
picture than that list suggests, and — critically — it revealed that the access surfaces split
into two categories with **opposite** correct handling. A prose runbook cannot enforce that split;
a reader following instructions is exactly the failure mode we need to design against.

**Access surface 1 — Keycloak is the identity hub.** The `ol-platform-engineering` realm
(`src/ol_infrastructure/substructure/keycloak/ol_platform_engineering.py`) declares **ten** OIDC
clients, all of which federate interactive login from it:

| Keycloak client | Service |
| --- | --- |
| `ol-vault-client` | Vault |
| `ol-grafana-client` | Grafana (with role mapper → admin/editor/viewer client roles) |
| `ol-concourse-client` | Concourse |
| `ol-dagster-client` | Dagster |
| `ol-airbyte-client` | Airbyte |
| `ol-jupyterhub-client` | JupyterHub |
| `ol-opik-client` | Opik |
| `ol-toolhive-client` | ToolHive |
| `ol-leek-client` | Leek |
| `ol-gwarek-client` | Gwarek |

Disabling one Keycloak user therefore revokes interactive access to ten services in one action.
The realm sets `registration_email_as_username=True`, so username == email address, which makes an
email address a sufficient and unambiguous identity key for this surface. Keycloak **users** are
not Pulumi-declared (only realms, roles, clients and mappers are), so users are pure
Admin-REST-API territory.

Vault reinforces this: GitHub auth was deliberately removed and OIDC-via-Keycloak is the only
interactive path, with an explicit "do not re-add GitHub auth" comment at
`src/ol_infrastructure/substructure/vault/auth/__main__.py:69`.

**Access surface 2 — Pulumi-managed, hardcoded human identity.** Two places encode humans
directly in infrastructure code:

- `src/ol_infrastructure/lib/aws/iam_helper.py` holds four hardcoded username lists —
  `ADMIN_USERNAMES`, `DEVOPS_ADMIN_USERNAMES`, `EKS_ADMIN_USERNAMES`, `EKS_DEVELOPER_USERNAMES` —
  consumed by `infrastructure/aws/iam`, `infrastructure/aws/eks`, `infrastructure/aws/opensearch`
  and `applications/concourse/iam_policies/pulumi_infra.py`.
- `src/ol_infrastructure/saas/rootly/__main__.py` holds hardcoded numeric Rootly `user_id`s in
  team membership (`user_ids=[99415, 100683, 103372, 103392]`) and in on-call escalation policy
  member positions.

For these, an API deletion is actively harmful: the next `pulumi up` would recreate the access, and
in the AWS case a deleted-out-of-band IAM user breaks the `iam.GroupMembership` resource. The
correct action is a **code change plus `pulumi up`**, which a script can identify precisely but must
not perform.

### Problem Statement

An offboarding procedure must (a) be fast and complete enough to use under urgent-termination
pressure, (b) be *verifiable* — someone must be able to confirm access is gone — and (c) never
take an API action against a Pulumi-managed resource. A prose runbook satisfies none of these
reliably: it drifts from the code it describes, it produces no artifact proving what was done, and
it puts the never-API-delete-Pulumi-managed-resources rule in the reader's head rather than in the
tooling.

### Business/Technical Drivers

- Departing staff currently retain access to whatever nobody remembered to check.
- The ten-service Keycloak fan-out means the highest-value action is also the easiest to script,
  and is currently undocumented anywhere.
- Vault's OIDC `readonly` and `developer` roles carry **no** `bound_claims` (only `admin` is
  claim-gated at `src/ol_infrastructure/substructure/vault/auth/__main__.py:165`), so any authenticated realm user can
  mint a Vault token. Combined with an 8-hour `token_ttl`, a Keycloak disable alone leaves a live
  Vault session valid for up to 8 more hours. Nothing in the repo records that gap today.

### Constraints

- **Verification must be non-destructive.** We cannot test an offboarding tool by offboarding
  someone. Correctness has to come from HTTP-mocked unit tests plus read-only `discover()` runs
  against QA/non-prod.
- **Pulumi-managed identity is off-limits to the API path**, per the two surfaces above.
- Credentials for each service differ (Keycloak admin password, Vault token, AWS profile, Rootly
  API token); the tool must degrade to a partial run rather than fail closed when one is absent,
  because an urgent offboard should not stall on a missing Rootly token.
- Repo conventions: single-file executable under `scripts/` (see `scripts/where-is-my-pr`),
  `cyclopts` for CLI, `httpx` for HTTP (see `scripts/keycloak_user_org_manager.py`).

### Assumptions

- Email address is the primary identity key. It resolves Keycloak directly. It resolves AWS only
  by *convention* (MIT Kerberos localpart == IAM username, e.g. `cpatti@mit.edu` → `cpatti`) —
  a convention the tool must verify rather than trust.
- Rootly numeric user IDs cannot be derived from an email offline; they require an API lookup.
- Sentry, Grafana Cloud org membership, and password-manager/SaaS consoles have no
  Pulumi-managed or in-repo human-access surface, so they are irreducibly manual. Their console
  URLs are still derived from stack configuration wherever the repository declares them — the
  Grafana stack hosts from the Keycloak client's allowed web origins and the MongoDB Atlas
  organization from `mongodb_atlas:organization_id` — so a manual step is still a clickable link
  that cannot drift from the deployed stacks.

### Options Considered

1. **Prose runbook, as literally requested in #5235**
   - Pros: cheapest to write; no credentials or blast radius; covers systems that have no API.
   - Cons: drifts silently from `iam_helper.py` and the Rootly user IDs the moment either changes;
     produces no record of what was actually revoked; relies on a human to know that deleting an
     AWS IAM user by hand is wrong; unverifiable.

2. **Fully automated offboarding, including the Pulumi-managed surfaces**
   - Pros: single action, nothing left to a human.
   - Cons: requires either API-deleting Pulumi-managed identities (which `pulumi up` then reverts,
     and which breaks `iam.GroupMembership`) or having the script commit code and run `pulumi up`
     itself — an unreviewed automated production deploy during a security-sensitive event. Rejected
     on both counts.

3. **Executable script, dry-run by default, that revokes what is safe to revoke via API and
   *reports* what is not** — with findings classified into API-revoked / needs-code-change /
   needs-a-human, plus a thin residual doc for the genuinely manual tail.
   - Pros: the never-touch-Pulumi-managed rule is encoded in the tool, not in the operator;
     dry-run output doubles as the always-current runbook, so it cannot drift from the code;
     every run produces an auditable artifact; the ten-service Keycloak win is one command.
   - Cons: more work than prose; needs credentials for four services; the manual tail still needs
     a human, so this does not fully eliminate the runbook — it shrinks it.

## Decision

### Chosen Option

Option 3. Build `scripts/admin/offboard-employee` — a single-file `cyclopts` executable taking one
or more email addresses — and reduce the #5235 runbook to a thin residual document covering only
what no API can reach.

### Rationale

The decisive factor is that the two access surfaces require opposite handling, and that asymmetry
is a *property of this codebase* discoverable only by reading it. Encoding it in a tool makes it
enforced; encoding it in prose makes it advisory. Secondarily, generating the runbook from a
dry-run means the documentation is derived from the code rather than maintained alongside it,
which is the only way it stays true as `iam_helper.py` and the Rootly user IDs change.

### Key Implementation Details

**Provider protocol.** Each service is a provider implementing two methods:

- `discover(identity) -> list[Finding]` — strictly read-only, always safe to run.
- `revoke(finding, *, execute: bool) -> ActionRecord` — emits an identically-shaped record whether
  or not it mutates, so dry-run and execute output are diffable.

**Three-way outcome taxonomy.** Every finding classifies into exactly one of:

- `REVOKED_VIA_API` — the tool can and did (or would) revoke it.
- `NEEDS_CODE_CHANGE` — requires a repo edit plus `pulumi up`. The finding must name the exact
  file, symbol, and stack(s) to re-deploy.
- `NEEDS_HUMAN` — a UI-only action, with the console URL and the owning team.

**Safety model.**

- Dry-run is the **default**. Mutation requires an explicit `--execute`.
- `--execute` additionally requires interactive confirmation echoing the resolved human identity
  per service, unless `--yes` is passed. The prompt explicitly lists incomplete provider discovery.
- AWS execution additionally requires `--aws-account-id`; the discovered IAM user's ARN must match
  that explicitly confirmed 12-digit account before any credential can be revoked.
- `--only` / `--skip` accept service names, so a partial or resumed run is possible.
- A missing credential or unresolved identity for one service is reported as incomplete and returns
  a non-zero exit status, but it does not prevent known urgent containment actions in other services.

**Per-service plan.**

| Service | `REVOKED_VIA_API` | `NEEDS_CODE_CHANGE` | `NEEDS_HUMAN` |
| --- | --- | --- | --- |
| Keycloak (`ol-platform-engineering`) | disable user, kill sessions, drop realm/client role mappings, drop federated identities — cascades to all ten clients | — | — |
| Vault | revoke the OIDC entity's live token accessors (closes the ≤8h `token_ttl` window Keycloak alone leaves open), then disable the entity while preserving aliases for audit and safe retries | — | — |
| AWS IAM | ephemeral credentials only: access keys, console login profile, MFA devices | remove from the relevant `iam_helper.py` list + `pulumi up` on `aws/iam`, `aws/eks`, `aws/opensearch`, `concourse` | — |
| Rootly | user deactivation, **if** the API supports it (open question below) | remove `user_id` from `saas/rootly/__main__.py` team membership and escalation positions + `pulumi up` | reassign open incidents |
| GitHub org | — | — | org/team membership removal |
| Sentry, Grafana stacks, Fastly, Heroku, MongoDB Atlas, Qdrant Cloud, Mailgun | — | — | console removal, each with a deep-linked console URL |
| Password manager | — | — | console removal; vendor is not declared in this repo, so no URL is emitted |

Note the AWS split is deliberate: the script revokes credentials (immediate, reversible,
not Pulumi-managed) and reports group membership (Pulumi-managed). Note also that
`EKS_DEVELOPER_USERNAMES` differs from the other three lists — those users are *declared* as
`iam.User` resources at `infrastructure/aws/iam/__main__.py:65`, so removing a name from that list
destroys the user, whereas removing a name from `ADMIN_USERNAMES` only drops a group membership.
The reported code-change instruction must say which.

**Verification.** HTTP-mocked unit tests for each provider's `discover()` parsing and `revoke()`
request construction, plus read-only `discover()` runs against QA. No test may mutate.

### Open Questions To Resolve During Implementation

- Does Rootly's REST API expose user deactivation? The `pulumi_rootly` provider has a
  `GetUser` data source but **no** `User` resource, confirming Pulumi cannot manage Rootly users
  either way. If the API has no deactivation verb, Rootly's user-level action becomes
  `NEEDS_HUMAN` while team membership stays `NEEDS_CODE_CHANGE`. Resolve with a read-only
  `GET /v1/users` probe.
- Email → AWS IAM username resolution relies on the Kerberos-localpart convention. The tool must
  confirm the derived username exists in IAM and report an unresolved-identity finding rather than
  guessing or silently skipping.

## Consequences

### Positive Consequences

- The highest-leverage action — disabling one Keycloak user, which cuts interactive access to ten
  services — becomes a single documented command instead of tribal knowledge.
- The ≤8-hour live-Vault-token gap after a Keycloak disable is closed explicitly rather than
  being an unrecorded hazard.
- Dry-run output is a runbook generated from the code, so it cannot drift from `iam_helper.py` or
  the Rootly user IDs.
- Every offboard produces an auditable record of what was and was not revoked.
- The rule "never API-delete a Pulumi-managed identity" moves from operator memory into tooling.

### Negative Consequences

- Substantially more work than writing prose, and the manual tail still needs a documented
  procedure — this shrinks the runbook rather than eliminating it.
- The tool needs credentials for four services; whoever runs it holds significant privilege.
- The `NEEDS_CODE_CHANGE` path is inherently slower than an API call: a PR plus `pulumi up`. For
  urgent terminations, the Keycloak + credential-revocation path is the fast containment and the
  code change is follow-up cleanup. The tool's output ordering should make that sequencing obvious.
- A script that is not run is worse than a runbook that is read; this needs an owner and a place
  in the offboarding process, not just a merge.

### Neutral Consequences

- The email → AWS username convention becomes load-bearing and therefore worth stating explicitly
  somewhere.
- `iam_helper.py`'s hardcoded lists and Rootly's hardcoded user IDs remain the underlying design
  problem. This ADR does not fix that; it makes the cost visible on every offboard. A future ADR
  could move human AWS access to Keycloak-federated SSO and delete the lists, which would collapse
  most of the `NEEDS_CODE_CHANGE` column.

## Implementation Notes

- **Effort Estimate:** 2–3 days for CLI, safety model, and the Keycloak/Vault/AWS providers;
  Rootly and the residual doc follow.
- **Risk Level:** High blast radius by nature, mitigated by dry-run-default and by never mutating
  Pulumi-managed resources.
- **Dependencies:** Keycloak admin credentials, a Vault token, an AWS profile and explicitly
  confirmed account ID for execution, and a Rootly API token.
- **Migration Path:** N/A — new tooling. The residual doc replaces the runbook ask in #5235.

## Related Decisions

- [0010. Pingdom Checks Unmanaged in Pulumi State](0010-pingdom-checks-unmanaged-in-pulumi-state.md) —
  same underlying theme of real-world state diverging from Pulumi's model.

## References

- `src/ol_infrastructure/substructure/keycloak/ol_platform_engineering.py` — realm, ten OIDC clients
- `src/ol_infrastructure/substructure/vault/auth/__main__.py` — OIDC backend, role `bound_claims`, TTLs
- `src/ol_infrastructure/lib/aws/iam_helper.py` — the four hardcoded username lists
- `src/ol_infrastructure/infrastructure/aws/iam/__main__.py` — group memberships and declared `iam.User`s
- `src/ol_infrastructure/saas/rootly/__main__.py` — hardcoded numeric user IDs
- `scripts/where-is-my-pr`, `scripts/keycloak_user_org_manager.py` — script conventions being followed

---

**Review History:**

| Date | Reviewer | Decision | Notes |
| --- | --- | --- | --- |
|  |  |  |  |

**Last Updated:** 2026-08-04
