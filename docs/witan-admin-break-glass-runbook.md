# witan admin / break-glass runbook

How to run a witan schema or data migration against a deployed environment, who
it runs as, and how an operator gets an interactive session of their own.

Covers agent-kit ADR-0005 path (a) (`witan login` from a laptop, per-user
identity) and path (b) (`svc-witan-admin`, in-cluster, no per-user identity).

## The two paths, and how to pick

|  | path (a) — `witan login` | path (b) — `svc-witan-admin` |
| --- | --- | --- |
| Identity | your own `act-<sub>` | one service principal |
| Runs from | your laptop | inside the cluster |
| Use it for | ad hoc reads/writes, "what does the graph say" | schema apply, store merge, storage rebuild, cross-actor debugging |
| Can it write another user's rows? | on the memory graph, yes — it is a shared graph | same, plus schema |
| Can it reindex / promote / delete a code-graph view? | no | no |

The rule: **if the operation has a per-user identity, use (a).** Path (b) exists
only for operations that genuinely have none — they act on the store as a whole.
Those are not MCP tools, so the remote proxy refuses them with a "run in-cluster"
error rather than doing something surprising.

## Path (a): interactive, as yourself

Requires the `witan-cli` public OIDC client in the `ol-platform-engineering`
realm (`substructure/keycloak/ol_platform_engineering.py`) and membership of that
realm — which is also what gets you an omnigraph token at all, see
`witan-token-sync-runbook.md`.

```bash
export WITAN_REMOTE_URL=https://witan.<env>.ol.mit.edu   # no <env> in Production
export WITAN_OIDC_ISSUER=https://sso-<env>.ol.mit.edu/realms/ol-platform-engineering
witan login          # prints a code + URL; complete it in a browser
witan whoami         # confirms the actor id the service resolved you to
```

`witan-cli` is the client id the CLI defaults to
(`witan_core.remote.config.DEFAULT_CLIENT_ID`), so there is nothing else to
configure. The token is cached at `~/.config/witan/tokens.json` (0600) and
refreshed transparently; `witan logout` drops it.

Two things that go wrong here, both with unhelpful-looking symptoms:

- **`invalid audience` / 401 from the vMCP.** The access token must carry
  `aud: witan`, which comes from the `witan-audience` protocol mapper on the
  `witan-cli` client. If the witan stack's `witan:oidc_audience` was overridden
  for an environment, that mapper's hard-coded `witan` no longer matches — they
  have to move in the same change.
- **`witan migrate …` refused.** Working as intended: those are path (b)
  operations. The error names what to do instead.

## Path (b): `svc-witan-admin`, in-cluster

### What it can and cannot do

Its Cedar grant (agent-kit `mcp/servers/witan/policy/`, ADR-0002 D4 as amended)
is deliberately asymmetric:

- **memory graph** — read, export, invoke_query, change, schema_apply. `change`
  is unavoidable: `witan migrate topics` / `repo-keys` / `merge` rewrite existing
  rows, and Cedar's finest scope is graph + branch, so "only rows nobody else
  owns" cannot be expressed. It grants nothing a human user does not already have
  on that graph — the only addition over a user actor is schema.
- **per-repo code graphs and the bridge** — read, export, invoke_query,
  schema_apply, and nothing else. No `change` (those graphs are re-derivable: the
  fix for a bad index is a reindex), no `branch_merge` (promotion into `main` is
  CI's, with a git merge behind it), no `branch_delete` (Cedar cannot tell whose
  WIP view a delete targets).
- **`omnigraph repair` / `optimize` / `cleanup` /
  `rebuild-full-text-indexes`** — not this principal, not this
  namespace. Those are direct-storage commands gated by AWS IAM on the bucket and
  scheduled by (or run beside) the omnigraph stack. See
  `omnigraph-store-maintenance-runbook.md`.

  `rebuild-full-text-indexes` is the one that misleads, because it *does* take
  `--as svc-witan-admin` — it is actor-bound, so the actor attributes the write.
  That does not make it a Cedar-authorized operation or a break-glass one: it
  still needs the bucket IRSA grant and so runs on the `omnigraph-server`
  ServiceAccount in the `omnigraph` namespace, not in a pod here.

### Which identity is an environment actually on?

```bash
pulumi stack output maintenance_actor_id --stack <CI|QA|Production>   # in applications/witan
```

`svc-witan-ci` means the admin principal is **not provisioned yet** for that
environment and maintenance is still borrowing the code-graph pipeline's
credential. That works only because the deployed `cluster.yaml` declares no
`policies:` block — the same Job is denied the moment one is applied, because
`witan-ci` has no grant on the memory graph at all.

### Provisioning it (per environment)

1. **Mint a token and put it in SOPS.** Two keys, and they must match — the
   omnigraph stack refuses the deploy otherwise, in either direction:

   ```bash
   openssl rand -hex 32     # the token value
   sops src/bridge/secrets/omnigraph/secrets.<env>.yaml
   ```

   ```yaml
   ci_token: <unchanged>
   admin_token: <new value>
   actor_tokens:
     svc-witan-ci: <unchanged>
     svc-witan-admin: <the same new value>
   ```

   It must also differ from `ci_token`: one shared value would make the two
   identities indistinguishable to the server while looking separate in the file.

2. **Deploy the omnigraph stack.** This writes
   `secret-operations/witan/admin-token`, adds the actor to
   `secret-operations/witan/service-tokens`, and exports
   `admin_token_provisioned: true`.

   The new entry has to reach `actor-tokens` and then omnigraph-server before the
   token authenticates anywhere. Two cases:

   - **token sync off** — Pulumi writes `actor-tokens` directly; the VSO picks up
     the change and restarts omnigraph-server (`rolloutRestartTargets`), because
     the server hashes its token map once at boot and never re-reads it. Expect a
     brief data-tier outage: it is single-replica with `strategy=Recreate`.
   - **token sync on** — the hourly CronJob merges service-tokens into
     `actor-tokens`. Nothing works until it runs; force it if you do not want to
     wait:

     ```bash
     kubectl -n omnigraph create job witan-token-sync-manual --from=cronjob/witan-token-sync
     kubectl -n omnigraph logs job/witan-token-sync-manual
     ```

3. **Deploy the witan stack.** It picks up `admin_token_provisioned`, syncs the
   `witan-admin-token` Secret, switches the pre-deploy migration Job onto it, and
   declares the suspended `witan-break-glass` CronJob.

4. **Verify.**

   ```bash
   pulumi stack output maintenance_actor_id     # -> svc-witan-admin
   kubectl -n witan get secret witan-admin-token
   kubectl -n witan get cronjob witan-break-glass       # SUSPEND must be True
   kubectl -n witan logs job/<the migration job>        # exits 0, not 401
   ```

   A 401 in the migration Job means step 2's half landed but the server has not
   picked up the map — check that omnigraph-server restarted after the Secret
   changed, and that the `svc-witan-admin` entry is actually in `actor-tokens`
   (not only in `service-tokens`).

### Running a break-glass operation

The `witan-break-glass` CronJob is never scheduled (`suspend: true`, plus a
schedule of 31 February that cannot fire). It exists to carry a pod spec —
image digest, ClusterIP address, graph id, token, service account — that a
runbook should not be restating by hand. Instantiate it:

```bash
# 1. Start the pod. Its default command is a 4-hour sleep, so it comes up idle.
kubectl -n witan create job witan-bg-$(date +%s) --from=cronjob/witan-break-glass
kubectl -n witan get pods -l app.kubernetes.io/name=witan-break-glass

# 2. Run the operation inside it.
kubectl -n witan exec -it job/witan-bg-<...> -- witan migrate schema

# ...or get a shell and poke around.
kubectl -n witan exec -it job/witan-bg-<...> -- /bin/sh

# 3. Done? End it rather than waiting out the sleep.
kubectl -n witan delete job witan-bg-<...>
```

**`kubectl create job --from=… -- <command>` does not work** — kubectl rejects it
outright (`error: cannot specify --from and command`), and a Job's pod template is
immutable once created, so there is no patching it afterwards. Exec into the idle
pod; that is the flow this template is shaped for, and it is the bastion-pod half
of path (b).

If you genuinely need an unattended one-shot, override the command before the Job
is submitted rather than after:

```bash
kubectl -n witan create job witan-bg-$(date +%s) --from=cronjob/witan-break-glass \
    --dry-run=client -o json \
  | jq '.spec.template.spec.containers[0].command = ["witan","migrate","schema"]' \
  | kubectl -n witan create -f -
```

Finished pods are kept for a week — they are the record of a manual intervention,
and `kubectl logs` on them is how the next person finds out what was done.

Things worth knowing before you run one:

- **`witan migrate storage` rebuilds the store and drops commit history and
  branches**, keeping a `.pre-migrate` backup. It is not a routine operation. It
  prompts for confirmation, which `kubectl exec -it` can answer; run it any other
  way (no TTY) and it aborts on `EOF` unless you pass `--yes`.
- **`witan migrate merge` needs its source store reachable from the pod.** A
  local `.omni` directory on your laptop is not; copy it in or export/load it
  through S3 first (Lance embeds absolute paths — export and load, never `mv`).
- **`witan migrate schema` against a cluster-managed graph duplicates what the
  omnigraph stack's `cluster apply` already does.** Reach for it when that path
  is unavailable — mid-upgrade, or the service is unhealthy *because* its schema
  is stale — not as routine convergence.
- **Do not un-suspend the CronJob.** Nothing here should run on a timer; the two
  operations that should are already CronJobs in the omnigraph stack.

### Rotating the token

Edit both SOPS keys to the same new value, redeploy the omnigraph stack, and
confirm omnigraph-server restarted (token sync off) or force a sync run (token
sync on). Nothing long-lived holds the old value: both consumers read it fresh at
pod start.

## Related

- `witan-council-probe-runbook.md` — `svc-witan-probe`, the same opt-in shape,
  for synthetic monitoring rather than maintenance.
- `witan-token-sync-runbook.md` — per-user tokens, and the `actor-tokens` /
  `service-tokens` writer split this builds on.
- `omnigraph-store-maintenance-runbook.md` — the *other* kind of maintenance
  (`repair`/`optimize`/`cleanup`), gated by IAM rather than Cedar.
- `adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md`; agent-kit
  `docs/adr/0005-secure-cli-path-into-deployed-witan.md` and
  `docs/adr/0002-witan-cedar-authorization-bundle.md`.
