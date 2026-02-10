# Vault policy for OCW Studio Kubernetes deployment

# AWS credentials for S3 and MediaConvert access
path "aws-mitx/creds/ocw-studio-app-*" {
  capabilities = ["read"]
}

# PostgreSQL database credentials
path "postgres-ocw-studio-applications-DEPLOYMENT/creds/app" {
  capabilities = ["read"]
}
path "postgres-ocw-studio-applications-DEPLOYMENT/creds/app/*" {
  capabilities = ["read"]
}

# OCW Studio application secrets
path "secret-ocw-studio/*" {
  capabilities = ["read"]
}
path "secret-ocw-studio" {
  capabilities = ["read"]
}

# Global secrets (mailgun)
path "secret-global/data/mailgun" {
  capabilities = ["read"]
}

# Concourse credentials for CI/CD integration
path "secret-concourse/ocw/api-bearer-token" {
  capabilities = ["read"]
}
path "secret-concourse/web" {
  capabilities = ["read"]
}

# Vault-secrets-operator lease management
path "sys/leases/renew" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-ocw-studio-applications-DEPLOYMENT/creds/app/*", "aws-mitx/creds/ocw-studio-app-*"]
  }
}
path "sys/leases/revoke" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-ocw-studio-applications-DEPLOYMENT/creds/app/*", "aws-mitx/creds/ocw-studio-app-*"]
  }
}
