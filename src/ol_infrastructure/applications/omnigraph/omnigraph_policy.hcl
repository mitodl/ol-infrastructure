# Vault policy for the omnigraph namespace's VSO sync.
#
# The path below is the kv-v1 form. `secret-operations` is a kv version 1
# mount (confirmed against vault-ci: `type=kv options={"version":"1"}`), and
# the OLVaultK8SStaticSecretConfig in __main__.py sets `mount_type="kv-v1"` to
# match. There is deliberately no `secret-operations/data/...` twin: that is
# the kv-v2 read path, and under kv-v1 `data/` is not an indirection but a
# literal path segment — a grant on it reads nothing the app uses, and would
# grant read on whatever anyone stored there later.

# {actor_id: token} JSON map — the artifact omnigraph-server boots its
# bearer-token auth from (OMNIGRAPH_SERVER_BEARER_TOKENS_FILE). The same
# Vault source witan resolves per-user tokens from (WITAN_ACTOR_TOKENS_FILE)
# in its own namespace. Seeded with at least the svc-witan-ci entry; per-user
# entries are written by the Keycloak witan-users sync (follow-up, not yet
# built — see applications/omnigraph/__main__.py).
path "secret-operations/witan/actor-tokens" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
