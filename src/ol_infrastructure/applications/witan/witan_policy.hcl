# svc-witan-ci: the single shared bearer token used for automated
# main-branch code-graph writes (ADR-0009 decision point 3). Also the value
# witan's own module-level fallback OmnigraphClient authenticates as
# (WITAN_MEMORY_TOKEN) when a request has no per-actor JWT in scope.
path "secret-operations/witan/ci-token" {
  capabilities = ["read"]
}

path "secret-operations/data/witan/ci-token" {
  capabilities = ["read"]
}

# {actor_id: token} JSON map — the same artifact omnigraph-server boots its
# bearer-token auth from (OMNIGRAPH_SERVER_BEARER_TOKENS_FILE) and witan
# resolves per-user tokens from (WITAN_ACTOR_TOKENS_FILE). Seeded here with at
# least the svc-witan-ci entry; per-user entries are written by the
# Keycloak witan-users sync (tk-... follow-up, not yet built — see
# applications/witan/__main__.py).
path "secret-operations/witan/actor-tokens" {
  capabilities = ["read"]
}

path "secret-operations/data/witan/actor-tokens" {
  capabilities = ["read"]
}

# PEM private key of the GitHub App the CI indexer clones as, letting it reach
# private repos (agent-kit witan_code/github_app.py). Seeded from
# src/bridge/secrets/witan/secrets.<env>.yaml by this stack, which is the sole
# writer of this path. Absent in environments with no App registered, where the
# indexer clones anonymously and this path is simply never read.
# No `secret-operations/data/witan/github-app` twin, unlike the two paths
# above. That form is the kv-v2 read path, and this mount is kv-v1 (see
# `mount_type="kv-v1"` on every OLVaultK8SStaticSecretConfig in __main__.py) —
# under kv-v1 `data/...` is not an indirection but a literal, different path,
# so granting read on it grants read on whatever someone stores there later.
# The two above predate this and are left alone rather than changed blind;
# they are dead grants by the same argument and worth removing separately.
path "secret-operations/witan/github-app" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
