# Vault policy for Digital Credentials Consortium services
# Grants read access to KV secrets for signing-service and issuer-coordinator

# Read access to signing service secrets (tenant keys)
path "secret-digital-credentials/data/signing-service" {
  capabilities = ["read"]
}

# Read access to issuer coordinator secrets (API tokens, tenant tokens)
path "secret-digital-credentials/data/issuer-coordinator" {
  capabilities = ["read"]
}

# Allow lease renewal for dynamic secrets if needed in future
path "sys/leases/renew" {
  capabilities = ["update"]
}
