# `svc-witan` — the MCP serving tier's service account

The third and last of witan's non-human principals, alongside `svc-witan-ci`
(the code-graph pipeline) and `svc-witan-admin` (break-glass maintenance).

## What it is for

The serving tier asks the omnigraph-server a small number of questions *about the
server* rather than of a graph. The one that matters is `omnigraph graphs list`:

- `witan_code.store.ensure_store` runs it before a write, to confirm the cluster
  actually declares the target graph.
- It backs the `code_indexed_repos` MCP tool.

That listing is server-scoped — Cedar action `graph_list`, bound to
`Omnigraph::Server::"root"` — so it belongs to no actor and happens *before*
`witan_code.ingest._client` swaps in the requesting user's token. It therefore
authenticates as the service or not at all, and an omnigraph-server booted with
`OMNIGRAPH_SERVER_BEARER_TOKENS_FILE` rejects an absent token.

Until this account existed the tier borrowed `svc-witan-ci` for it. That was not
a privilege escalation — the actor swap in `_client` is unconditional and
`WITAN_CODE_INDEX_ROLE` stays `client`, so the CI role's one real privilege
(writing a graph's shared default-branch view) was never reachable from the tier
— but it attributed the serving tier's enumerations to the data pipeline, and it
meant rotating the CI token took the tier's code-graph reads down with it.

## Why it is required, not optional

`svc-witan-admin` is opt-in: an environment without it keeps running maintenance
as `svc-witan-ci`, which is a working arrangement. **`svc-witan` is not.**

The bundles are applied unconditionally in every environment (see
`src/ol_infrastructure/applications/omnigraph/cluster_config.py`), and the
image entrypoint renders
group membership from the live actor-token map at boot
(agent-kit `mcp/servers/witan/policy/render_groups.py`). An environment whose
token map has no `svc-witan` entry gets a `witan-service` group with no members,
which the renderer **drops** — along with every rule naming it.

So the MCP tier is granted nothing it needs: `graph_list` is denied, which fails
`witan_code.store.ensure_store` ahead of every code-graph write and makes
`code_indexed_repos` return nothing. The data tier itself is healthy.

**The symptom is a working server with a broken tier, not a crash-loop.** Worth
stating precisely, because it changed:

- Before agent-kit **#188**, the renderer emitted the empty group and
  `omnigraph-policy` rejected it outright — `policy group 'witan-service' must
  not be empty` (`omnigraph-policy/src/lib.rs:302`) — raised *after* the
  `serving omnigraph` log line, so it read like a healthy start right up until
  the pod restarted. That took CI down on 2026-08-06, between the bundles
  landing and this account being provisioned.
- #188 taught the renderer to drop unprovisioned groups instead. The outage is
  gone; the misconfiguration is now silent.

Which is exactly why the omnigraph stack hard-fails the deploy when the keys are
missing. A crash-loop announces itself. A dropped group looks like a successful
deploy until someone notices code-graph writes failing, so the check has to
happen before it ships rather than after.

## Provisioning it (per environment)

1. **Mint a token and put it in SOPS.** Two keys, and they must match:

   ```bash
   openssl rand -hex 32
   sops src/bridge/secrets/omnigraph/secrets.<env>.yaml
   ```

   ```yaml
   ci_token: <unchanged>
   admin_token: <unchanged>
   service_token: <new value>
   actor_tokens:
     svc-witan-ci: <unchanged>
     svc-witan-admin: <unchanged>
     svc-witan: <the same new value>
   ```

   It must differ from **both** `ci_token` and `admin_token`, and the stack
   refuses the deploy otherwise. Cedar tells these principals apart by actor id
   alone, so a shared value is one principal holding the union of both sets of
   grants while the bundle still shows them as separate groups. That matters
   most here: the serving tier is the only principal holding a credential while
   serving user traffic.

   Non-interactively (which never prints the value):

   ```bash
   tok="$(openssl rand -hex 32)"
   f=src/bridge/secrets/omnigraph/secrets.<env>.yaml
   sops set "$f" '["service_token"]' "\"${tok}\""
   sops set "$f" '["actor_tokens"]["svc-witan"]' "\"${tok}\""
   unset tok
   ```

2. **Deploy the omnigraph stack.** This writes
   `secret-operations/witan/service-token`, adds the actor to
   `secret-operations/witan/service-tokens`, and exports
   `service_token_provisioned: true`.

   The entry has to reach `actor-tokens` and then omnigraph-server before it
   authenticates anywhere — the server hashes its token map once at boot and
   never re-reads it. With token sync **on** (every environment, as of
   2026-08-05), the stack's own bootstrap Job now folds the service-tokens map
   into its re-run trigger (`token_sync.py::create_token_sync`,
   `tk-close-the-witan-service-token-deploy-order-windo-91b403`), so adding an
   actor here re-runs the merge as part of THIS `pulumi up` — no manual force
   step needed. If you are on a checkout predating that fix, or want to
   confirm the merge landed without waiting on the deploy log, force it
   directly:

   ```bash
   kubectl -n omnigraph create job witan-token-sync-manual --from=cronjob/witan-token-sync
   kubectl -n omnigraph logs job/witan-token-sync-manual
   ```

   The `actor-tokens` VaultStaticSecret carries
   `rolloutRestartTargets: [omnigraph-server]`, and its own spec now also
   changes on every service-tokens fingerprint change (same task), so the
   Vault Secrets Operator has something to react to immediately rather than
   only its 15-minute `refresh_after` poll. The bootstrap Job's merge is
   confirmed to land before `pulumi up` reports success; the VSO's own
   render-and-restart is not something Pulumi waits on directly, so treat
   "the omnigraph stack deployed" as "very likely done, worth confirming"
   rather than "definitely done" — see Verifying below.

3. **Deploy the witan stack.** It picks up `service_token_provisioned` and moves
   the `witan-code-token` Secret from `witan/ci-token` to `witan/service-token`.
   The MCPServer spec is unchanged: the credential already had its own Secret
   precisely so this move would be a one-line path change. This Secret also
   now carries a `rolloutRestartTargets: [witan-server]` and the same
   service-tokens fingerprint annotation as step 2, for the same reason —
   without it, the running `witan-server` pod (which reads this credential
   once into a `WITAN_CODE_TOKEN` env var, not a re-statted file) would keep
   presenting whatever token it started with.

## Verifying

```bash
# The group is populated
kubectl -n omnigraph logs deploy/omnigraph-server | grep render-policy-groups
#   ... memory.policy.yaml [witan-users=33, witan-service=1, witan-admin=1]

# The tier is using its own identity — ask the VaultStaticSecret which Vault
# path it syncs from. The Secret's own name never changes, so `get secret` tells
# you nothing; the spec is where ci-token and service-token differ.
kubectl -n witan get vaultstaticsecret witan-code-token -o jsonpath='{.spec.path}{"\n"}'
#   witan/service-token      <- provisioned
#   witan/ci-token           <- still borrowing the pipeline's credential

pulumi stack output service_token_provisioned --stack <CI|QA|Production>
```

Nothing above prints secret data: `.spec.path` is configuration, and the
VaultStaticSecret spec holds no token.

**`READY 1/1` is not evidence.** Since agent-kit #188 the server starts fine
whether or not this account exists, so deployment health tells you nothing here.
The render line is the check that matters, and the failure signature is the
group being **absent** from it — a dropped group is not logged as
`witan-service=0`, it simply does not appear:

```
# provisioned
... memory.policy.yaml [witan-users=33, witan-service=1, witan-admin=1]
# NOT provisioned — note what is missing, not what is zero
... memory.policy.yaml [witan-users=33, witan-admin=1]
```

The renderer also says so outright, on stderr:

```bash
kubectl -n omnigraph logs deploy/omnigraph-server | grep "unprovisioned group"
#   render-policy-groups: WARNING unprovisioned group(s): ['witan-service'] —
#   dropped from every bundle along with the rules referencing them, so those
#   actions are granted to nobody
```

### Silence from those greps is NOT a pass

Both lines are printed **once, at boot**, and `omnigraph-server` logs every
Lance operation at INFO. On a server doing real work the boot output rotates
out of the retained log within about ten minutes, after which *both* greps
return nothing whether or not the account exists — the failing state and the
passing state become indistinguishable.

This is a live trap, not a theoretical one: it produced a confident false pass
during this account's own rollout, on a server that had in fact never been given
the token.

**Confirm the boot output is still there before trusting either grep:**

```bash
kubectl -n omnigraph logs deploy/omnigraph-server | grep -c "render-policy-groups"
```

`0` means the log rotated and you have learned nothing. `4` (one line per
bundle) means the window is still open and the greps above are meaningful.

**When it has rotated, read the token map instead.** It is the input the
renderer works from, so it answers the question directly and does not expire:

```bash
kubectl -n omnigraph get secret actor-tokens -o go-template='{{index .data "tokens.json"}}' \
  | base64 -d | jq 'keys | map(select(startswith("act-") | not))'
#   [ "svc-witan", "svc-witan-admin", "svc-witan-ci" ]
```

Piping through `jq 'keys'` keeps the tokens themselves off your terminal and out
of your shell history; do not `cat` that Secret.

Restarting the pod to regenerate the boot lines also works, but costs a
data-tier outage to answer a question the Secret already answers.

End to end, the thing that actually proves it works is an enumeration through
the tier — `code_indexed_repos` returning repos rather than nothing.

## Rotating

Same as minting: change both keys to a new value, deploy the omnigraph stack,
then deploy witan. Because the two keys are checked against each other, a
half-rotation fails the deploy rather than reaching the cluster.

Rotation is the harder case, and worth calling out specifically: unlike
minting, `witan-code-token`'s Vault *path* does not change on rotation (it
already points at `witan/service-token`), so there is no CR spec edit to
carry the update for free the way the mint's path swap does. Both
`actor-tokens` (omnigraph stack) and `witan-code-token` (witan stack) now
carry a service-tokens content-fingerprint annotation for exactly this
reason (`tk-close-the-witan-service-token-deploy-order-windo-91b403`) — a
rotated value changes the fingerprint, which changes each Secret CR's own
spec, which is what gives the Vault Secrets Operator something to act on
immediately instead of only its `refresh_after` poll (15m for
`actor-tokens`, 1h for `witan-code-token`). Both Secrets also now carry a
`rolloutRestartTargets` entry, so once the VSO renders the new value each
Deployment restarts on it rather than a running pod silently keeping the old
token in memory.

None of that is something `pulumi up` blocks on directly, though — the VSO's
own convergence happens outside Pulumi's await. Verify the same way as
Verifying above (`render-policy-groups`, or an end-to-end
`code_indexed_repos` call) rather than trusting a clean `pulumi up` alone,
especially soon after a rotation.

## Related

- `docs/witan-admin-break-glass-runbook.md` — `svc-witan-admin`, same shape,
  opt-in rather than required.
- `docs/witan-council-probe-runbook.md` — `svc-witan-probe`, also opt-in,
  for synthetic monitoring rather than maintenance.
- `docs/witan-token-sync-runbook.md` — how service tokens reach `actor-tokens`.
- agent-kit `mcp/servers/witan/policy/` — the Cedar bundles and the boot-time
  membership renderer.
