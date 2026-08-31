# toolhive_swe no longer syncs any Vault-held secret: MCP clients authenticate
# directly against Keycloak (no vMCP-side embedded auth server or upstream client
# secret), and the `aws` backend authenticates via IRSA, not a Vault-issued token.
# OLEKSAuthBindingConfig is still called (rather than dropped) because it is also
# what provisions the `aws` backend's IRSA trust role and ServiceAccount — that
# path is independent of Vault. This file stays non-empty only because the
# component's config validator requires exactly one of vault_policy_path /
# vault_policy_text, and unconditionally provisions a Vault Policy + Kubernetes
# auth backend role + Vault Secrets Operator VaultConnection/VaultAuth alongside
# the IRSA wiring, whether or not anything in the namespace still reads from it.
path "sys/leases/renew" {
  capabilities = ["update"]
}
