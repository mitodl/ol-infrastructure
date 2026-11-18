path "postgres-label-studio/creds/app" {
  capabilities = ["read"]
}

# Keycloak OIDC client credentials for the APISIX openid-connect plugin.
# secret-operations is a kv-v1 mount.
path "secret-operations/sso/label-studio" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
