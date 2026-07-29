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

path "sys/leases/renew" {
  capabilities = ["update"]
}
