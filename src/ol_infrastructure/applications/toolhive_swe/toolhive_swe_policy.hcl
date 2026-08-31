# toolhive_swe no longer syncs any Vault-held secret: MCP clients authenticate
# directly against Keycloak (no vMCP-side embedded auth server or upstream client
# secret), and the `aws` backend authenticates via IRSA, not a Vault-issued token.
# This policy is retained (rather than dropping the OLEKSAuthBindingConfig call
# entirely) only because that component still provisions the Vault Secrets
# Operator wiring the `aws` backend's ServiceAccount plumbing depends on.
path "sys/leases/renew" {
  capabilities = ["update"]
}
