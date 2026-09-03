# `svc-witan-probe` — the council-health synthetic-monitoring account

The fourth of witan's non-human principals, alongside `svc-witan-ci` (the
code-graph pipeline), `svc-witan` (the MCP serving tier), and `svc-witan-admin`
(break-glass maintenance).

## What it is for

Two alert rules already cover a lost `council` graph, from opposite
directions, and a real window sits between them:

- `WitanToolCallErrorRate` (`metric_rules/witan.py`) needs live MCP traffic to
  say anything — with none in the window it is `0/0 = NaN` and never fires.
- `WitanGraphQuarantined` (`log_rules/witan.py`) needs a pod restart — it reads
  `graph_count` off omnigraph-server's own boot line.

`council` lost during a quiet period with no restart is invisible to both
until someone tries to use the service, which for a shared agent memory graph
can be hours. Neither existing health check can be deepened to close that gap
without breaking what it is *for*: omnigraph's `/healthz` is flat and
unauthenticated by design, and witan's own probe deliberately answers from
process state alone (deepening it converts backend slowness into frontend
death — see `applications/witan/deployment.py`).

So this is a separate, out-of-band, **authenticated** caller:
`applications/omnigraph/council_probe.py` runs a CronJob
(`check_council_health.py`) on its own schedule, in its own pod, carrying its
own Cedar identity. It runs one trivial read against `council` and exits
non-zero if that read did not come back — nothing more. See the script's own
module docstring for the wire-level detail.

**It needs no new alert rule.** A failing run *is* the signal:
`WitanScheduledJobNeverSucceeded` and `eks_general.py`'s `WorkloadJobFailed*`
already alert on a failing CronJob, and the CronJob's name is in
`eks_general.py`'s fast staleness bucket for the "stopped running entirely"
case those miss.

## Why a fourth identity, not one of the other three

`svc-witan-ci` is permanently excluded from the memory graph by design (it is
a code-graph data pipeline with no role on the work graph — see
`memory.policy.yaml`'s header). `svc-witan-admin` is break-glass and should
not authenticate on a schedule. `svc-witan` is the serving tier's own
credential, live in every real user request, not something a monitoring job
should hold or rotate in lockstep with. `svc-witan-probe` costs nothing on any
graph but `memory`, and there it holds `read` + `invoke_query` only — no
`export`, no `change`, no `schema_apply`. See agent-kit
`mcp/servers/witan/policy/README.md` § "Non-human actors".

## Why it is optional, like `svc-witan-admin`

Unlike `svc-witan` (required in every environment — see
`witan-service-account-runbook.md`), an environment whose SOPS file has no
`probe_token` simply has no council-health CronJob deployed. There is no
crash-loop and no denied-but-authenticated state to reason about, because
nothing authenticates as this identity at all until the token exists.

## Provisioning it (per environment)

1. **Mint a token and put it in SOPS.** Two keys, and they must match, and the
   value must differ from `ci_token`, `admin_token`, and `service_token` — the
   stack refuses the deploy otherwise, for the same reason as every other
   identity here: Cedar tells these principals apart by actor id alone, so a
   shared value collapses two principals into one holding the union of their
   grants.

   ```bash
   tok="$(openssl rand -hex 32)"
   f=src/bridge/secrets/omnigraph/secrets.<env>.yaml
   sops set "$f" '["probe_token"]' "\"${tok}\""
   sops set "$f" '["actor_tokens"]["svc-witan-probe"]' "\"${tok}\""
   unset tok
   ```

2. **Deploy the omnigraph stack.** This writes
   `secret-operations/witan/probe-token`, adds the actor to
   `secret-operations/witan/service-tokens`, syncs a Kubernetes Secret from
   the first path via the Vault Secrets Operator, and creates the
   `witan-council-probe` CronJob in the `omnigraph` namespace — all in this
   one stack; unlike `svc-witan`/`svc-witan-admin` there is no witan-stack
   half to deploy separately, since the probe talks to omnigraph-server
   directly rather than through the MCP tier.

   The entry has to reach `actor-tokens` and then omnigraph-server before it
   authenticates anywhere — the server hashes its token map once at boot and
   never re-reads it. With token sync on (every environment, as of
   2026-08-05), the stack's own bootstrap Job folds the service-tokens map
   into its re-run trigger, so this lands within the same `pulumi up`. If you
   want to confirm without waiting on the deploy log:

   ```bash
   kubectl -n omnigraph create job witan-token-sync-manual --from=cronjob/witan-token-sync
   kubectl -n omnigraph logs job/witan-token-sync-manual
   ```

## Verifying

```bash
# The group is populated
kubectl -n omnigraph logs deploy/omnigraph-server | grep render-policy-groups
#   ... memory.policy.yaml [witan-users=33, witan-admin=1, witan-probe=1]

# The CronJob exists and has succeeded at least once
kubectl -n omnigraph get cronjob witan-council-probe
kubectl -n omnigraph logs -l app.kubernetes.io/name=witan-council-probe --tail=20
```

Same caveat as every other identity here: the boot-log render line rotates
out of the retained log within about ten minutes on a busy server. Read the
token map instead once it has:

```bash
kubectl -n omnigraph get secret actor-tokens -o go-template='{{index .data "tokens.json"}}' \
  | base64 -d | jq 'keys | map(select(startswith("act-") | not))'
#   [ "svc-witan", "svc-witan-admin", "svc-witan-ci", "svc-witan-probe" ]
```

**End-to-end acceptance for this task specifically**: make the probe fail for
a reason other than `council` actually being down (e.g. temporarily point
`OMNIGRAPH_GRAPH_ID` at a graph id that does not exist), and confirm the
CronJob-failure alert path fires — not by reading the manifest.

## Rotating

Same as minting: change both keys to a new value and deploy the omnigraph
stack. Each CronJob run mounts the Secret fresh in a brand-new pod, so unlike
the always-running data-tier Deployment there is nothing to restart — the very
next scheduled run picks up the rotated token.

## Related

- `docs/witan-admin-break-glass-runbook.md` — `svc-witan-admin`, same
  opt-in shape.
- `docs/witan-service-account-runbook.md` — `svc-witan`, the required
  counterpart, and the fullest write-up of the render-groups verification
  trap that applies here too.
- agent-kit `mcp/servers/witan/policy/` — the Cedar bundles (`witan-probe`
  group in `memory.policy.yaml`) and the boot-time membership renderer.
