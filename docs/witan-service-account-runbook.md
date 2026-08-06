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
as `svc-witan-ci`. **`svc-witan` is not.**

witan's Cedar bundles all declare a `witan-service` group, and
`omnigraph-policy` refuses to start the server on a group with no members:

```
Error:
   0: policy group 'witan-service' must not be empty
   crates/omnigraph-policy/src/lib.rs:302
```

The bundles are applied unconditionally in every environment (see
`applications/omnigraph/cluster_config.py`), and the image entrypoint renders
group membership from the live actor-token map at boot
(agent-kit `mcp/servers/witan/policy/render_groups.py`). So an environment whose
token map has no `svc-witan` entry produces an empty `witan-service` group and a
**crash-looping data tier** — the server logs `serving omnigraph` and then exits,
which reads like a healthy start right up until the pod restarts.

This is not hypothetical: it took CI down on 2026-08-06 between the Cedar bundles
landing and this account being provisioned.

The omnigraph stack therefore hard-fails the deploy when the keys are missing,
so the message names the missing SOPS keys instead of leaving an operator to
work backwards from a CrashLoopBackOff.

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
   2026-08-05) the hourly CronJob merges service-tokens into actor-tokens; force
   it rather than waiting:

   ```bash
   kubectl -n omnigraph create job witan-token-sync-manual --from=cronjob/witan-token-sync
   kubectl -n omnigraph logs job/witan-token-sync-manual
   ```

   The `actor-tokens` VaultStaticSecret carries
   `rolloutRestartTargets: [omnigraph-server]`, so the server restarts itself
   once the map changes and re-renders `witan-service` with a member. Expect a
   brief data-tier outage — single replica, `strategy=Recreate`.

3. **Deploy the witan stack.** It picks up `service_token_provisioned` and moves
   the `witan-code-token` Secret from `witan/ci-token` to `witan/service-token`.
   The MCPServer spec is unchanged: the credential already had its own Secret
   precisely so this move would be a one-line path change.

## Verifying

```bash
# The group is populated and the server stayed up
kubectl -n omnigraph logs deploy/omnigraph-server | grep render-policy-groups
#   ... memory.policy.yaml [witan-users=33, witan-service=1, witan-admin=1]
kubectl -n omnigraph get deploy omnigraph-server   # READY 1/1, no restarts

# The tier is using its own identity
kubectl -n witan get secret witan-code-token -o jsonpath='{.metadata.name}'
pulumi stack output service_token_provisioned --stack <CI|QA|Production>
```

A `witan-service=0` in that log line is the failure this account exists to
prevent — the server will exit immediately after.

## Rotating

Same as minting: change both keys to a new value, deploy the omnigraph stack,
force the sync job, then deploy witan. Because the two keys are checked against
each other, a half-rotation fails the deploy rather than reaching the cluster.

## Related

- `docs/witan-admin-break-glass-runbook.md` — `svc-witan-admin`, same shape,
  opt-in rather than required.
- `docs/witan-token-sync-runbook.md` — how service tokens reach `actor-tokens`.
- agent-kit `mcp/servers/witan/policy/` — the Cedar bundles and the boot-time
  membership renderer.
