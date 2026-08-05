# witan token sync runbook

How per-user omnigraph bearer tokens are provisioned from Keycloak, how to turn
the sync on for an environment, and what to do when it misbehaves.

## What it is

Every witan user gets their own omnigraph bearer token, keyed by the actor id
derived from their Keycloak `sub` (agent-kit ADR-0004 D3). The `witan-token-sync`
CronJob in the `omnigraph` namespace is what mints and retires them:

```
secret-operations/witan/actor-tokens
    = secret-operations/witan/service-tokens
    + one act-<sub> entry per enabled, non-service-account user of the realm
```

- **Onboarding a user is one action:** add them to the `ol-platform-engineering`
  realm. Within an hour they have a token.
- **Offboarding is the same action in reverse** — remove them from the realm, or
  disable the account. Either retires the token on the next run.
- **Nobody ever sees these tokens.** A user authenticates to the witan MCP tier
  with an OIDC JWT; witan maps their `sub` to `act-<sub>`, looks the token up,
  and presents it to omnigraph-server on their behalf. There is nothing to
  distribute and nothing for a user to configure.

**There is no `witan-users` Keycloak group, deliberately.** ADR-0004 D3 names
one, but `witan-users` is a *Cedar* group in agent-kit's policy bundles
(`mcp/servers/witan/policy/server.policy.yaml`), populated with the `act-<sub>`
ids this job writes — the name collision is what made a Keycloak group of the
same name look mandatory. The realm has `registration_allowed=False`, no
identity-provider brokering and no federation, so it is already limited to
exactly the intended audience. A group inside it would be a second gate whose
failure mode is somebody joining the realm, nobody adding them to the group, and
a 401 that reads like a provisioning lag. The trade accepted: **no way to revoke
witan while leaving this realm's other applications (jupyterhub, superset, opik)
intact** — realm access is witan access.

Clients' own service accounts (`service-account-*`, e.g. `ol-opik-client` and
this job's own `witan-token-sync`) are realm users too, and are skipped. The
non-human actors that *do* get tokens come from the service map, which is
declared in SOPS rather than discovered in Keycloak.

Source: `src/ol_infrastructure/applications/omnigraph/token_sync.py` (deployment)
and `.../omnigraph/scripts/sync_actor_tokens.py` (the reconciliation itself).

## Ownership of the two Vault paths

| Path | Writer | Contents |
| --- | --- | --- |
| `secret-operations/witan/service-tokens` | the omnigraph Pulumi stack, from `src/bridge/secrets/omnigraph/secrets.<env>.yaml` | non-human actors (`svc-witan-ci`, and `svc-witan-admin` where provisioned — see `witan-admin-break-glass-runbook.md`) |
| `secret-operations/witan/actor-tokens` | the `witan-token-sync` CronJob | the merged map both omnigraph-server and the witan tier read |

One writer per path, and they must stay that way. A second writer on
`actor-tokens` reverts every per-user entry on each `pulumi up`, which 401s
every user until the next hourly run and restarts omnigraph-server at both ends
of that window.

Until an environment has the sync turned on, the Pulumi stack writes
`actor-tokens` itself (there are no per-user entries yet, so the merged map and
the service map are the same thing).

## Turning it on for an environment — two steps, in this order

The switch is `omnigraph:keycloak_url`. Setting it moves ownership of
`actor-tokens` from Pulumi to the CronJob.

**Prerequisite:** the `witan-token-sync` Keycloak client must exist in the target
realm. It comes from the keycloak substructure stack, so deploy
`ol-infrastructure-substructure-keycloak` for that environment first and confirm
`secret-operations/witan/token-sync-oidc` is populated:

```shell
vault kv get secret-operations/witan/token-sync-oidc
```

**Step 1 — deploy the code with the switch still off.**

```shell
cd src/ol_infrastructure/applications/omnigraph
pulumi up --stack <CI|QA|Production>
```

This adds `service-tokens`, and — the part that matters — records
`retainOnDelete` against the existing `actor-tokens` Pulumi resource. Pulumi
reads that flag from state at deletion time, and a resource absent from the
program is never re-registered, so it has to be recorded *before* step 2.

**Step 2 — set the switch and deploy again.**

```shell
pulumi config set omnigraph:keycloak_url https://sso-<env>.ol.mit.edu
pulumi up --stack <CI|QA|Production>
```

Pulumi drops the `actor-tokens` resource from state without touching the Vault
path, and the bootstrap Job takes over an already-populated one. Expect roughly
10 new resources: a ServiceAccount, a Vault policy and Kubernetes auth role, the
OIDC-credential VaultStaticSecret, the script ConfigMap, the bootstrap Job and
the CronJob.

**Doing both steps in one `pulumi up` deletes the Vault path.** The bootstrap Job
rewrites it later in the same run, but nothing orders the deletion against the
job, so there is a window in which omnigraph-server has no valid token for
anybody.

Verify afterwards:

```shell
kubectl -n omnigraph get cronjob witan-token-sync
kubectl -n omnigraph logs job/$(kubectl -n omnigraph get jobs \
  -l app.kubernetes.io/name=witan-token-sync -o name | head -1 | cut -d/ -f2)
vault kv get -field=tokens_json secret-operations/witan/actor-tokens | jq 'keys'
```

## Other configuration

| Key | Default | Notes |
| --- | --- | --- |
| `omnigraph:keycloak_url` | unset (sync off) | the switch |
| `omnigraph:keycloak_realm` | `ol-platform-engineering` | |
| `omnigraph:token_sync_schedule` | `17 * * * *` | see the cost note below before shortening |

## Why hourly

Every write to `actor-tokens` trips the VSO `rolloutRestartTarget` on that
secret, and the data tier is `replicas=1` + `strategy=Recreate` — a hard ~10-30s
graph outage, absorbed by connect-failure retry in the agent-kit client. So the
schedule is not bounded by Keycloak's cost (two API calls) but by how often a
restart is acceptable. Steady state is free: an unchanged membership produces a
byte-identical map and the job writes nothing at all, so the restart cost is
paid only on real membership churn.

## Running it by hand

```shell
kubectl -n omnigraph create job --from=cronjob/witan-token-sync token-sync-manual
kubectl -n omnigraph logs job/token-sync-manual -f
```

For a read-only check of what it *would* do, add `WITAN_TOKEN_SYNC_DRY_RUN=1`:

```shell
kubectl -n omnigraph create job --from=cronjob/witan-token-sync token-sync-dryrun \
  --dry-run=client -o json \
  | jq '.spec.template.spec.containers[0].env += [{"name":"WITAN_TOKEN_SYNC_DRY_RUN","value":"1"}]' \
  | kubectl apply -f -
```

## Rotating one user's token

Rotation is deliberately manual — the job carries an existing member's token
over verbatim, because re-minting on a schedule would restart omnigraph-server
every interval. To force a rotation, delete that actor's entry and let the next
run re-mint it:

```shell
vault kv get -field=tokens_json secret-operations/witan/actor-tokens \
  | jq 'del(."act-<sub>")' \
  | jq -R '{tokens_json: .}' \
  | vault kv put secret-operations/witan/actor-tokens -
kubectl -n omnigraph create job --from=cronjob/witan-token-sync token-sync-rotate
```

## Troubleshooting

**A new user still gets "No omnigraph bearer token provisioned for actor …".**
Check, in order: are they in the realm and enabled; has the CronJob run
since; did it write; has the VSO propagated. The error text comes from
agent-kit's `ActorTokenResolver` and means the map on disk has no such key.

```shell
kubectl -n omnigraph get jobs -l app.kubernetes.io/name=witan-token-sync
kubectl -n omnigraph get secret actor-tokens -o jsonpath='{.data.tokens\.json}' \
  | base64 -d | jq 'keys'
```

Note the two-sided lag: witan's resolver re-reads the file on any cache miss, so
it picks up a new entry immediately, but omnigraph-server hashes the map once at
boot and only sees it after the VSO-triggered restart. A user who gets past
witan and then 401s from the graph is in that window.

**The job fails with "Service token map … is empty or missing".** It refused to
write, which is the intended behaviour — the merged map is a whole-secret
replace, so an empty service map would silently retire `svc-witan-ci` and break
the CI indexer and witan's fallback client. Fix `service-tokens` (it comes from
the SOPS file via `pulumi up`), then re-run.

**The job fails with "returned no users at all".** Also deliberate: a failed
lookup and a genuinely empty realm are indistinguishable in the response but not
in consequence, and the job will not retire every per-user token on the strength
of one. The usual cause is the `witan-token-sync` service account having lost its
`view-users` realm-management role — re-deploy the keycloak substructure stack.

**403 from Vault.** The paths in `token_sync_policy.hcl` must match
`ACTOR_TOKENS_VAULT_PATH` / `SERVICE_TOKENS_VAULT_PATH` in `token_sync.py`
verbatim. They are duplicated between an HCL file and a Python constant, and a
mismatch shows up only at runtime.

**Everyone's token changed at once.** Something re-minted rather than carried
over — check whether `actor-tokens` was emptied or made unreadable before a run.
The `wrote N actors to …` line appears in the job log only when the map actually
changed; if it appears every hour, that is the symptom.
