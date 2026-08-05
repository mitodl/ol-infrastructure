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
# in its own namespace. Read-only, and read-only it stays: the witan-users sync
# job that writes this path authenticates as its own Vault role with its own
# policy (token_sync_policy.hcl), precisely so the write capability does not
# land on the identity every VSO sync in this namespace uses.
path "secret-operations/witan/actor-tokens" {
  capabilities = ["read"]
}

# OIDC credentials for the `witan-token-sync` Keycloak service account, written
# by the keycloak substructure stack and rendered into the token-sync job's
# environment by the VSO. Read by the operator, not by omnigraph-server.
path "secret-operations/witan/token-sync-oidc" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
