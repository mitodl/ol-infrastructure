# Keycloak upstream OIDC client secret for ToolHive's embedded authorization
# server (spec.authServerConfig.upstreamProviders[].oidcConfig.clientSecretRef).
# secret-operations is a kv-v1 mount; include both path forms defensively.
path "secret-operations/sso/toolhive" {
  capabilities = ["read"]
}

path "secret-operations/data/sso/toolhive" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
