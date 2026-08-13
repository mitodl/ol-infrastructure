# Vault policy for the witan-users token-sync job (scripts/sync_actor_tokens.py).
#
# Deliberately NOT the `omnigraph-server` policy this namespace already has.
# That one is read-only and is what the Vault Secrets Operator authenticates
# with; this job is the one workload here that WRITES, and giving the operator's
# identity a write capability to save a role would hand it to every VSO sync in
# the namespace. Separate ServiceAccount, separate Vault role, separate policy.
#
# kv-v1 paths, matching `secret-operations`' mount type — see the note in
# omnigraph_policy.hcl for why there is no `secret-operations/data/...` twin.

# The job's own output: the merged {actor_id: token} map that omnigraph-server
# boots its bearer-token auth from and the witan tier resolves per-user tokens
# through. `read` as well as write because the job carries an existing member's
# token over verbatim rather than re-minting it — without the read it could only
# ever rewrite the whole map with fresh tokens, invalidating every live session
# on every run and bouncing omnigraph-server with it.
path "secret-operations/witan/actor-tokens" {
  capabilities = ["read", "create", "update"]
}

# The job's input: the Pulumi/SOPS-owned map of non-human actors (svc-witan-ci,
# and svc-witan-admin where an environment has provisioned it) that gets merged
# into every write of the path
# above. Read-only here — this job is not the writer of that path, and the
# separation of the two writers is the whole point of the split.
path "secret-operations/witan/service-tokens" {
  capabilities = ["read"]
}
