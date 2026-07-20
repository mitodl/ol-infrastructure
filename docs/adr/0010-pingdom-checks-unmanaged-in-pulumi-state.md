# 0010. Pingdom Checks Unmanaged in Pulumi State (Dynamic Provider Import Limitation)

**Status:** Proposed
**Date:** 2026-07-20
**Deciders:** Infrastructure team
**Technical Story:** Grafana Alerting migration, #4828 (Phase 1 — Pingdom uptime checks via Pulumi dynamic provider)

## Context

### Current Situation

`src/ol_infrastructure/infrastructure/grafana_alerting/pingdom_checks.py` manages 39 Pingdom
uptime checks through a **Pulumi dynamic provider** (`pulumi.dynamic.Resource` /
`ResourceProvider`) — a Python class (`_PingdomCheckProvider`) whose `create()`, `read()`,
`update()`, and `delete()` methods call the Pingdom v3 REST API directly via `requests`. This
runs only from the Production Pulumi stack, since Pingdom checks are account-wide.

All 39 checks were successfully migrated from legacy, manually-created Pingdom checks to this
Pulumi-managed set. **The checks are live, correctly configured, and alerting properly through
Rootly.** This was verified multiple times directly against the Pingdom API: 42 total checks in
the account (39 migrated + 3 deliberately left untouched — a dead test check, a dead xPro
preview check, and a superseded paused MITx CMS check), zero unexpected duplicates, zero
coverage gaps against the 39 declared targets.

**However, Pulumi's own state for the Production stack has zero record of any of these 39
checks.** `pulumi stack export` shows only the 25 non-Pingdom resources (contact points, folders,
notification policy, and the 15 metric/log `RuleGroup` resources), which deployed and
checkpointed normally — this issue is specific to the Pingdom dynamic-provider resource type.

### Problem Statement

Because Pulumi's state has no record of the 39 checks, **a plain, untargeted `pulumi up` on the
Production stack would attempt to create all 39 again from scratch** — real duplicate checks in
Pingdom, not just a bookkeeping mismatch, since Pulumi has no way to know they already exist.

The normal fix for "a resource exists in the real world but not in Pulumi state" is `pulumi
import`. That does not work here, for a specific, confirmed, structural reason (Issue 1 below).
Getting to this state also involved a separate, unexplained anomaly during the migration itself
(Issue 2 below), which is not blocking anything today but is worth documenting.

### Constraints Discovered Along the Way

- **Pingdom's plan has a hard cap of 50 checks per account** (confirmed via
  `GET /api/3.1/credits`: `defaultchecklimit: 50`). This meant the "create all 39 new checks,
  then delete the 39 old ones" plan was not viable as a single batch — old and new checks could
  never coexist for all 39 at once. The migration had to interleave delete-old/create-new,
  verifying each one via a direct Pingdom API call before proceeding, rather than trusting
  Pulumi's own reported success/failure (see Issue 2).

### Assumptions

- The 39 live Pingdom checks are the source of truth going forward; nothing here proposes
  re-touching them.
- Correctness (not creating duplicates, not corrupting shared Pulumi state) matters more than
  closing this gap quickly.

---

## Issue 1 — `pulumi import` does not work for Python dynamic providers (confirmed, structural)

### What we tested

Wired `opts=pulumi.ResourceOptions(import_="14553605")` (the real Pingdom ID for the
`airbyte-qa-http` check) onto that one resource and ran `pulumi preview --target <urn>`
(read-only, no mutation possible) against the real Production stack. Reproduced consistently,
including once run directly by a human (not through an AI-agent-mediated shell), ruling out any
tool/harness artifact:

```
pulumi-python:dynamic:Resource airbyte-qa-http import
error: Preview failed: Exception calling application: '__provider'
```

This is the org's own documented standard procedure for adopting existing resources into Pulumi
(code `import_=` option, review the diagnostic diff, add mismatches to `ignore_changes`, remove
the import option once adopted) — the failure isn't from deviating from that playbook, it's that
the playbook's diagnostic-diff step itself depends on a `Read()` call that Python dynamic
providers structurally cannot service during import (see Root cause).

### Root cause

Read directly from the installed SDK source
(`pulumi/dynamic/__main__.py`, function `get_provider`):

```python
def get_provider(props, config):
    ...
    providerStr = props[PROVIDER_KEY]   # PROVIDER_KEY = "__provider"
```

Every dynamic resource carries its entire CRUD implementation as a pickled (`dill`) Python object
attached to a hidden `__provider` property — this is what lets a "dynamic resource" avoid needing
a separately-compiled provider plugin. Every RPC handler (`Create`, `Read`, `Update`, `Delete`)
starts by unpickling this blob via `get_provider()`.

When Pulumi processes a resource with `import_=` set and no existing state entry, its first step
is to call `Read()` using whatever properties are already on record for that resource — which,
for a true from-scratch import, is nothing. There is no historical `__provider` blob to draw on
(it's only ever generated fresh by our own `_PingdomCheck.__init__` when the program runs
normally), and the import flow does not substitute the freshly-declared properties for this
initial Read call. `props['__provider']` genuinely is not there, and `KeyError('__provider')` is
raised and surfaces as `Exception calling application: '__provider'`.

This is a structural limitation of Pulumi's Python dynamic-provider SDK, not a bug in our code,
not fixable by retrying, and not specific to Pingdom — it would affect any Python dynamic
resource an operator tried to import.

---

## Issue 2 — `create()` sometimes reports failure while the real Pingdom check is still created (unexplained, not currently blocking)

### What happened during migration

Every `pulumi up`/`pulumi up --target` invocation that created a Pingdom check during the
migration reported an error (`400 Client Error: Bad Request` from our own `r.raise_for_status()`,
or a generic `KeyboardInterrupt`/`executor.shutdown` traceback from the Pulumi language host) —
**and the real check was nonetheless created correctly in Pingdom, every time.** Because Pulumi
believed the operation failed, nothing was checkpointed into state. This is the direct cause of
the current gap in Issue 1's problem statement — not the import limitation itself, but how we
arrived at 39 real, unmanaged checks.

We worked around this live by writing a verification loop: create via `pulumi up --target`,
independently confirm via a direct Pingdom API call that exactly one new check appeared, only
then delete the old check. This is why the 39 checks are correctly, verifiably live today despite
Pulumi's own reporting being wrong throughout.

### What we ruled out, with evidence

| Hypothesis | How it was tested | Result |
|---|---|---|
| Non-JSON-serializable value in `outs` | Code inspection of `create()` — `outs` contains only strings, ints, bools, lists, `None` | Ruled out |
| `CreateResult.id_` not a plain string | Code inspection — already explicitly `str()`-cast | Ruled out |
| A secondary "fetch full details" API call after create | Code inspection — `create()` makes exactly one HTTP call | Ruled out |
| Stale `pendingOperations` from earlier crashed bulk runs | Checked `pulumi stack export`'s `deployment.pendingOperations` directly on Production | Empty — ruled out |
| Pulumi Python SDK version drift (installed `3.250.0` vs. a freshly-resolved `3.253.0`) | Diffed `pulumi/dynamic/__main__.py` between both versions; byte-identical. Re-ran an isolated repro pinned to the exact production version (`3.250.0`) | Succeeded cleanly — ruled out |
| AI-agent tool/harness artifact (no TTY, buffering, timeouts) | A human ran the identical targeted command directly in their own terminal, outside any agent-mediated shell | Same failure reproduced — ruled out |

### What we could not reproduce, despite trying

Built an isolated, disposable Pulumi project (separate project/stack/local-file backend, no
contact with real Pulumi state) to test theories safely against the real Pingdom API. None of the
following reproduced the failure — every one succeeded cleanly, with a proper state checkpoint:

- A trivial no-op dynamic resource
- A dynamic resource with an artificial 2-second delay in `create()`
- A dynamic resource making a real Pingdom `GET` call
- A dynamic resource making a real Pingdom `POST /checks` call with the exact production resource
  shape (secret-wrapped token, list-typed `tags`/`probe_filters`/`integrationids`, a `None`-valued
  field)
- The same real `POST`, but with 38 additional declared (non-networked) dynamic resources in the
  same program and `--target` scoping to just the one real resource — mirroring production's
  actual shape (39 declared Pingdom checks, one targeted)

### What remains untested

The one structural difference between our repro and the real failing runs we did not test: the
real `__main__.py` also bootstraps the actual `pulumiverse_grafana.Provider` and creates real
`grafana:alerting` resources (contact points, rule groups) in the *same* update as the Pingdom
checks. Testing this properly would have required real Grafana credentials and more direct
production/CI contact than felt warranted for a bug that, at this point, is not blocking anything
— so this was deliberately left uninvestigated rather than continuing to escalate real-system
contact chasing it.

### Why this isn't currently urgent

The 39 checks that needed creating are created. This bug would only resurface if we needed to
create *new* Pingdom checks (e.g. adding a new monitoring target) through this same dynamic
provider mechanism in the future.

---

## Options Considered (for closing the Issue 1 state gap)

### Option A — Leave unmanaged, guard against accidental duplication (lowest cost)

Add an explicit safety check so `pingdom_checks.create()` cannot be invoked via a bare `pulumi up`
without deliberate opt-in, and document the gap clearly here and in
`grafana_alerting/CLAUDE.md`. The 39 checks keep working exactly as they do today.

- **Pros:** Zero risk, minutes of work, fully reversible.
- **Cons:** Doesn't actually close the gap. Pingdom checks stay outside normal `pulumi
  preview`/diff workflows indefinitely, until someone picks this up.

**This is the option implemented alongside this ADR** — see `pingdom_checks.py`, which now
requires `pulumi config set allow_pingdom_apply true` before `create()` will run at all.

### Option B — Hand-edit Pulumi state directly (`stack export` → inject → `stack import`)

Craft state entries for all 39 checks by hand (real Pingdom IDs, matching inputs, a `__provider`
blob borrowed from a cleanly-checkpointed donor resource of the same class) and import them into
the real Production state file.

- **Pros:** Fully closes the gap without touching Pingdom or requiring new tooling.
- **Cons:** Directly edits a shared, production Pulumi state file by hand — if the injected
  entry doesn't exactly match what Pulumi expects, the failure mode is a corrupted or
  inconsistent stack, which is worse than the current gap. **Rejected** during this
  investigation as too risky for the benefit.

### Option C — Fork and properly publish a Terraform Pingdom provider

`DrFaust92/terraform-provider-pingdom` already has everything we need: a `pingdom_check` resource
with fields matching ours (`name`, `host`, `resolution`, `tags`, `probefilters`, `integrationids`,
`paused`, etc.) and a real, standard Terraform `Importer`
(`schema.ImportStatePassthroughContext`) — confirmed by reading the Go source directly. Pulumi
can dynamically bridge any Terraform provider via `pulumi package add terraform-provider
<namespace>/<name>`.

This currently fails with `the provider is not signed with a valid signing key; please contact
the provider author (authentication signature from unknown issuer)`. Per HashiCorp's own
documentation on this exact error, it means the provider's published release's GPG signature
doesn't match a key registered against that publisher's registry namespace — not a Pulumi-side
allowlist of "blessed" providers. In principle a fork, republished under our own registry
namespace with our own properly-registered signing key, would pass this check.

- **Pros:** Reuses working, already-understood CRUD and import logic. No new Go/provider-SDK
  code to write.
- **Cons:** Requires becoming a real registry publisher — claiming a namespace, generating and
  registering a GPG key, standing up (or adapting the existing) release pipeline. A genuine,
  ongoing commitment (key custody, future maintenance, version bumps), not a one-off fix.

### Option D — Write a small native Pulumi provider from scratch

Use `pulumi-go-provider` to implement a proper, independently-compiled Pulumi provider for the
`Check` resource (Create/Read/Update/Delete/Diff), matching what `_PingdomCheckProvider` already
does. Reference the plugin locally (no Pulumi registry involvement, so the signing check in
Option C never applies).

- **Pros:** Fully solves both Issue 1 (real import support) and sidesteps Issue 2 entirely (a
  compiled provider doesn't have the pickled-provider-in-state problem). No dependency on any
  third party's registry namespace or signing.
- **Cons:** No Go toolchain currently available in this environment. Genuinely multi-day work
  even with one — new repo, learning the provider SDK, implementing and testing the full
  resource lifecycle.

---

## Decision

**Not yet made** on the long-term fix (Option C vs. D). **Option A is implemented now** as the
interim safety measure, since leaving the code able to silently attempt 39 duplicate creates on
the next `pulumi up` was not acceptable to leave open-ended.

Recommendation for the long-term fix, when there's bandwidth: **Option D**. It's the only option
that removes both issues at the root rather than working around Issue 1 while leaving Issue 2
latent for the next time someone needs to create a Pingdom check this way.

## Consequences

### Positive

- No risk to the 39 live, working checks.
- No further Pulumi state risk — `pulumi up` on Production can no longer silently attempt to
  recreate all 39 checks.
- Buys time to properly scope Option C or D as real, planned work.

### Negative (of leaving the gap open)

- Pingdom checks remain outside Pulumi's normal `preview`/diff/drift-detection workflow
  indefinitely, until Option C or D is completed.
- Anyone who does deliberately set `allow_pingdom_apply` to make an unrelated change (e.g. to
  add one new check) will hit the Issue 2 anomaly again and needs to fall back to the
  create-then-verify-via-API pattern described in Notes below, rather than trusting `pulumi up`'s
  own reported result.

### Neutral

- Issue 2 remains unexplained. It should be re-examined if Option D is pursued, since a native
  provider would need its own Create implementation tested for the same failure mode (though
  the mechanism that caused it — the pickled-provider/dynamic-resource RPC bridge — would no
  longer be present).

## Related Decisions

- None yet — this is the first ADR touching the Grafana Alerting / Pingdom migration
  (#4828, Phase 1).

## References

- [Pulumi dynamic providers — SDK source, `pulumi/dynamic/__main__.py`](https://github.com/pulumi/pulumi/blob/master/sdk/python/lib/pulumi/dynamic/__main__.py)
- [`DrFaust92/terraform-provider-pingdom` — `pingdom_check` resource docs](https://registry.terraform.io/providers/DrFaust92/pingdom/latest/docs/resources/check)
- [HashiCorp support — "Error: Failed to install provider - authentication signature from unknown issuer"](https://support.hashicorp.com/hc/en-us/articles/26638397057299-Error-Failed-to-install-provider-authentication-signature-from-unknown-issuer)
- [Pulumi docs — Any Terraform Provider](https://www.pulumi.com/docs/iac/get-started/terraform/terraform-providers/)
- [`pulumi-go-provider` — building native Pulumi providers](https://github.com/pulumi/pulumi-go-provider)
- [mitodl platform-engineering-site — Import existing resources with Pulumi](https://github.com/mitodl/platform-engineering-site/blob/main/docs/platform_services/cloud_infrastructure/import_existing_resources_with_pulumi.md) (the org's standard `import_=` playbook; does not cover Python dynamic providers)
- Pingdom API v3.1 — `GET /api/3.1/credits` (confirms the 50-check plan limit)

## Notes

The one-at-a-time, API-verified migration script used to safely complete the 39-check migration
was a throwaway script (not committed to the repo). Its logic, if useful as a reference for
future similar migrations or additions: create via `pulumi up --target <resource-urn>`,
regardless of Pulumi's own reported success/failure poll the target system directly for up to
~20 seconds for a newly appeared object, and only delete the corresponding old object once
independently confirmed — never trust Pulumi's own success/failure signal alone when a dynamic
provider is involved.

---

**Review History:**

| Date | Reviewer | Decision | Notes |
|------|----------|----------|-------|
| 2026-07-20 | | | Initial draft |

**Last Updated:** 2026-07-20
