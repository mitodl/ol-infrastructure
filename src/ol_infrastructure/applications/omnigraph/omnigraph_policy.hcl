# {actor_id: token} JSON map — the artifact omnigraph-server boots its
# bearer-token auth from (OMNIGRAPH_SERVER_BEARER_TOKENS_FILE). The same
# Vault source witan resolves per-user tokens from (WITAN_ACTOR_TOKENS_FILE)
# in its own namespace. Seeded with at least the svc-witan-ci entry; per-user
# entries are written by the Keycloak witan-users sync (follow-up, not yet
# built — see applications/omnigraph/__main__.py).
path "secret-operations/witan/actor-tokens" {
  capabilities = ["read"]
}

path "secret-operations/data/witan/actor-tokens" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
