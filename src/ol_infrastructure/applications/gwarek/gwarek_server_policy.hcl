# Vault policy for the Gwarek Kubernetes deployment

# PostgreSQL database credentials — app role for the running api/worker
# pods, admin role for the one-off migration Job (needs DDL privileges
# alembic upgrade head requires that the app role doesn't have).
path "postgresql-gwarek-DEPLOYMENT/creds/app" {
  capabilities = ["read"]
}
path "postgresql-gwarek-DEPLOYMENT/creds/admin" {  # pragma: allowlist secret
  capabilities = ["read"]
}

# Static application secrets (Anthropic API key, local credential-encryption key)
path "secret-gwarek/*" {
  capabilities = ["read", "list"]
}
path "secret-gwarek/data/*" {
  capabilities = ["read", "list"]
}

# Keycloak OIDC credentials for gwarek's APISIX route
path "secret-operations/sso/gwarek" {
  capabilities = ["read"]
}
