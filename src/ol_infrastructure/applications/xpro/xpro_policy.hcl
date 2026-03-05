# Vault policy for xPro Kubernetes deployment

# AWS credentials for S3 access
path "aws-mitx/creds/xpro-app" {
  capabilities = ["read"]
}

# PostgreSQL database credentials
path "postgres-xpro/creds/app" {
  capabilities = ["read"]
}
path "postgres-xpro/creds/app/*" {
  capabilities = ["read"]
}

# xPro application secrets
path "secret-xpro/*" {
  capabilities = ["read"]
}
path "secret-xpro" {
  capabilities = ["read"]
}

# Global secrets (mailgun)
path "secret-global/data/mailgun" {
  capabilities = ["read"]
}

# Vault-secrets-operator lease management
path "sys/leases/renew" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-xpro/creds/app/*", "aws-mitx/creds/xpro-app/*"]
  }
}
path "sys/leases/revoke" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-xpro/creds/app/*", "aws-mitx/creds/xpro-app/*"]
  }
}
