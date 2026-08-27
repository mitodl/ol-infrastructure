# 0011. Detect Fastly Drift by Name-Set Audit, Not by Pulumi Refresh

**Status:** Proposed
**Date:** 2026-08-27
**Deciders:** Platform Engineering
**Technical Story:** [hq#12449](https://github.com/mitodl/hq/issues/12449), [ol-infrastructure#5627](https://github.com/mitodl/ol-infrastructure/pull/5627)

## Context

### Current Situation

A VCL snippet named `Handle course/program redirects to MIT Learn` shipped in #5513. The
`/` is illegal in a Fastly snippet name, so the API returned 400 partway through the
apply — after Pulumi had already cloned a service version. The failed run still persisted
the full *desired* snippet set into the resource's state outputs. The provider's `SetDiff`
then read the never-created snippet back as `Unmodified`, so no subsequent `pulumi up`
could ever heal it.

QA and Production served without the redirect for days. Every deploy reported success.
The same class of failure recurred in #5563 with parentheses in two mit-learn cache-key
snippet names.

Nothing noticed, because nothing compares Pulumi's state to Fastly's reality. Every
Fastly-bearing stack runs with `refresh_stack=False` — set *because* the stack has Fastly
resources, since refresh calls the Fastly API with a token that goes stale mid-rotation
and fails the whole job. The trigger condition and the detection mechanism are the same
resource, so coverage is 0% exactly where the risk is 100%.

#5627 added `validate_vcl_snippet_name()` and closed the *ingress* for this specific
cause. It does nothing for state that is already wrong, for the other name-keyed child
collections, or for drift introduced by hand in the Fastly UI.

### Problem Statement

Detect, without a human in the loop, the condition "Pulumi state claims a named child
object exists on a Fastly service and the live service does not have it."

### Constraints

- `refresh_stack=False` is load-bearing today and its removal is tracked separately.
  Detection must not depend on that being fixed first.
- The check runs against Production. It must not be able to mutate state.
- The `mitodl/ol-infrastructure` image contains no Pulumi CLI.
- A nightly check that is not silent when things are healthy gets muted within a week.
  Zero standing false positives is a hard requirement, not an aspiration.

### Options Considered

1. **Nightly `pulumi preview --refresh`, alert on any non-empty diff**
   - Pros: Uses the tool we already have; catches every resource type, not just Fastly.
   - Cons: Measured on `ol-application-mit-learn/CI`, the refresh reports **38** resources
     with diffs, of which exactly **one** is Fastly. The other 37 are routine Kubernetes
     churn (13 `VaultStaticSecret`, 5 `VerticalPodAutoscaler`, 5 `Deployment`, 4 KEDA
     `ScaledObject`, and so on). Fires every night on noise.

2. **The same, filtered to Fastly resources**
   - Pros: Removes the Kubernetes churn.
   - Cons: Still permanently non-empty, and the signal is *inverted*. That one
     `ServiceVcl` refresh-diff is 189 lines containing no real drift at all: `backends`
     and `loggingHttps` are dropped and re-added because they contain `[secret]` members
     and the whole collection flips to secret on refresh, plus `requestSettings` gains a
     provider-default `maxStaleAge: 0`. Meanwhile `snippets` does not appear — because
     the snippets genuinely matched. The noise is unconditional and the thing we care
     about is invisible inside it.

3. **Purpose-built name-set audit against the Fastly API**
   - Pros: Compares exactly the property that broke. No refresh, no admin token, no
     ability to write state. Measured silent across the whole estate today.
   - Cons: Bespoke code to maintain; covers Fastly only; compares names, so an edit to a
     snippet's *body* is out of scope.

## Decision

**Chosen option: 3, a purpose-built name-set audit.**

For every `fastly:index/serviceVcl:ServiceVcl` resource in the estate, compare the set of
child-object *names* recorded in Pulumi state against the set of names the Fastly API
reports for the version currently serving traffic.

**Rationale.** Options 1 and 2 fail the zero-false-positive constraint by a wide margin,
and option 2 additionally hides the target signal inside its own noise. Option 3 is
decoupled from the `refresh_stack` work, so it ships now rather than after a credential
redesign.

### Key Implementation Details

- **State side.** Read the checkpoint directly from
  `s3://mitol-pulumi-state/.pulumi/stacks/<project>/<stack>.json` with boto3. The
  resource outputs carry `id`, `activeVersion`, and the name-keyed child collections.
  This needs no Pulumi CLI and no `PULUMI_CONFIG_PASSPHRASE`.
- **Live side.** `GET /service/<id>/version/<live active version>/<collection>` for
  `snippet`, `condition`, `header`, `domain`, `request_settings`, and `dictionary`.
  Authenticated with the existing read-only `global_read_api_key` from SOPS
  `fastly.yaml`, not the admin token.
- **Compare against the version Fastly is actually serving,** resolved from
  `/service/<id>/details`, rather than the version state believes is active. The question
  is what is serving traffic, not what we think is.
- **`DECLARED-BUT-ABSENT` fails the job.** That set difference is the #5513/#5563 failure
  mode exactly. `live-but-undeclared` and an `activeVersion` mismatch are reported but do
  not fail, since both are ordinary consequences of a manual UI edit.
- **Delivery:** a nightly Concourse pipeline in the Production `infra` pool, following
  `iam_drift`/`github_drift` in shape, alerting to Slack on failure. Unlike those two it
  does *not* open a pull request — there is no code change to propose. The remedy is the
  targeted `pulumi refresh` + `pulumi up` runbook, which the alert links to.

Three traps have to be handled or the check false-alarms permanently:

1. **Secret-wrapped collections.** `backends` and `loggingHttps` are frequently stored not
   as a list but as `{"4dabf18193072939515e22adb298388d": ..., "ciphertext": "v1:..."}` — <!-- pragma: allowlist secret -->
   that first key is Pulumi's public marker for a secret-wrapped value, not a credential.
   Iterating that yields zero names, and every live backend is then reported as drift.
   Detect the sigil and mark the collection unauditable rather than empty.
2. **Orphaned checkpoints.** The bucket holds 324 stack files; only 306 belong to projects
   this repo still declares. Dead projects `ol-infrastructure-fastly` and
   `ol-infrastructure-mitxonline-application` retain checkpoints pointing at the *same
   live service IDs* as the current `ol-application-*` stacks, hundreds of versions stale.
   Scope the scan to project names parsed from `src/**/Pulumi.yaml`, not to an S3 listing.
3. **`TlsSubscription` also has a `domains` output,** but its members are plain strings.
   Filter on resource type and skip non-dict members.

## Consequences

### Positive

- Closes the detection gap that let hq#12449 run for days. **Verified against the real
  incident:** Fastly versions are immutable, so mitxonline Production's declared snippets
  can be replayed against the pre-repair v207, which yields exactly
  `DECLARED-BUT-ABSENT: ['Redirect course and program pages to MIT Learn']`. Against the
  repaired v208 it is silent.
- **Measured silent today.** Full estate run: 36 `ServiceVcl` resources across 32 stacks,
  81 seconds, `ALERT=0`. The five informational findings are five unauditable `backends`
  collections and one benign `activeVersion` mismatch (state 14, live 16, all names
  matching) on `ol-application-edxapp/xpro.Production`.
- Requires **no new IAM and no new credentials.** The Production `infra` pool already has
  `s3:GetObject*`/`s3:ListBucket*` on the bucket via the `pulumi_state` policy module and
  `kms:Decrypt` via `infra`.
- Structurally incapable of mutating state — it only reads S3 and issues Fastly `GET`s
  with a read-only token.
- Also catches drift Pulumi cannot see at all, such as a snippet deleted by hand in the
  Fastly UI.

### Negative

- Compares names only. A changed snippet *body* under an unchanged name is not detected.
- `backends` and `loggingHttps` stay unauditable while they are secret in state.
- Bespoke code against an undocumented-but-stable detail: the Pulumi checkpoint JSON
  layout. A backend format change would break the reader. Mitigated by keeping the
  parsing in one place, and by the fact that a parse failure fails loudly.
- Detection latency is up to 24 hours.

### Neutral

- Coverage is 32 stacks, not the 8 an earlier estimate suggested: `edxapp` alone carries
  12 stacks and `ocw-site` holds three services per stack.
- Does not remove the need to fix Fastly token rotation and re-enable `refresh_stack`;
  it makes that work non-urgent rather than unnecessary.

## Implementation Notes

- **Effort Estimate:** 1–2 days. A validated prototype already produced the numbers above.
- **Risk Level:** Low — read-only, and it runs outside the deploy path.
- **Dependencies:** None blocking.

## Related Decisions

- [0010. Pingdom Checks Unmanaged in Pulumi State](0010-pingdom-checks-unmanaged-in-pulumi-state.md) — the
  same underlying theme of Pulumi state disagreeing with a third-party provider.
- ol-infrastructure#5627 — the preventive half: reject illegal snippet names at preview.
- platform-engineering-site#67 — the Fastly/Pulumi state-drift repair runbook the alert
  will point at.

## References

- [Fastly API: VCL snippets](https://www.fastly.com/documentation/reference/api/vcl-services/snippet/)
- `src/ol_concourse/pipelines/infrastructure/iam_drift/pipeline.py` — the nightly drift
  pipeline shape this follows.

---

**Review History:**

| Date | Reviewer | Decision | Notes |
|------|----------|----------|-------|
| | | | |

**Last Updated:** 2026-08-27
