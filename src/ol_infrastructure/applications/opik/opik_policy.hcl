path "secret-clickhouse/data/credentials" {
  capabilities = ["read"]
}

path "secret-clickhouse/metadata/credentials" {
  capabilities = ["read", "list"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}

# Keycloak OIDC client credentials for the APISIX openid-connect plugin
# (secret-operations is a kv-v1 mount; include both path forms defensively).
path "secret-operations/sso/opik" {
  capabilities = ["read"]
}

path "secret-operations/data/sso/opik" {
  capabilities = ["read"]
}
