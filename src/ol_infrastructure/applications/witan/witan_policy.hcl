# Vault policy for the witan namespace's VSO sync.
#
# Every path below is the kv-v1 form. `secret-operations` is a kv version 1
# mount (confirmed against vault-ci: `type=kv options={"version":"1"}`), and
# every OLVaultK8SStaticSecretConfig in __main__.py sets `mount_type="kv-v1"`
# to match. There are deliberately no `secret-operations/data/...` twins: that
# is the kv-v2 read path, and under kv-v1 `data/` is not an indirection but a
# literal path segment — a grant on it reads nothing the app uses, and would
# grant read on whatever anyone stored there later.

# svc-witan-ci: the single shared bearer token used for automated
# main-branch code-graph writes (ADR-0009 decision point 3). Also the value
# witan's own module-level fallback OmnigraphClient authenticates as
# (WITAN_MEMORY_TOKEN) when a request has no per-actor JWT in scope.
path "secret-operations/witan/ci-token" {
  capabilities = ["read"]
}

# svc-witan-admin: the break-glass maintenance principal (agent-kit ADR-0005 path
# b). Read by the pre-deploy migration Job and by the suspended break-glass
# CronJob in this namespace — never by anything that serves traffic, which is the
# point of it being a separate path from the map next door. Written by the
# omnigraph stack from the same SOPS source as ci-token; absent in environments
# that have not provisioned it yet, where this grant simply reads nothing.
path "secret-operations/witan/admin-token" {
  capabilities = ["read"]
}

# svc-witan: the MCP serving tier's own account, used for the server-scoped
# questions it asks before any per-actor token is in scope — `omnigraph graphs
# list`, which gates code-graph writes and backs code_indexed_repos (Cedar
# `graph_list`). Read by the witan-code-token Secret, which pointed at ci-token
# until this account existed. Written by the omnigraph stack from the same SOPS
# source as the other two. Unlike admin-token this is present in every
# environment that has a SOPS file at all, because the Cedar bundles'
# `witan-service` group cannot be empty.
path "secret-operations/witan/service-token" {
  capabilities = ["read"]
}

# {actor_id: token} JSON map — the same artifact omnigraph-server boots its
# bearer-token auth from (OMNIGRAPH_SERVER_BEARER_TOKENS_FILE) and witan
# resolves per-user tokens from (WITAN_ACTOR_TOKENS_FILE). Always carries the
# svc-witan-ci entry; per-user entries are written by the realm token-sync
# CronJob in the omnigraph stack (applications/omnigraph/token_sync.py), in
# environments that have it enabled. Read-only here either way — this stack is
# a consumer of that path, never a writer of it.
path "secret-operations/witan/actor-tokens" {
  capabilities = ["read"]
}

# PEM private key of the GitHub App the CI indexer clones as, letting it reach
# private repos (agent-kit witan_code/github_app.py). Seeded from
# src/bridge/secrets/witan/secrets.<env>.yaml by this stack, which is the sole
# writer of this path. Absent in environments with no App registered, where the
# indexer clones anonymously and this path is simply never read.
path "secret-operations/witan/github-app" {
  capabilities = ["read"]
}

# Sentry DSN for this workload, owned by the ol-infrastructure-sentry stack
# and written here by this stack (witan_sentry_vault_secret in __main__.py) —
# the sentry stack only exports the DSN as a stack output, it never writes
# Vault itself. Synced into the witan-sentry-secrets k8s Secret and read as
# SENTRY_DSN by the Deployment (deployment.py).
path "secret-operations/witan/sentry" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
