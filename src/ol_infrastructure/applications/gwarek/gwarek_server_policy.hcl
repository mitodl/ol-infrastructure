# Vault policy for the Gwarek Kubernetes deployment

# PostgreSQL database credentials — app role for the running api/worker
# pods, admin role for the one-off migration Job (needs DDL privileges
# alembic upgrade head requires that the app role doesn't have).
#
# Written as a literal, already-resolved path rather than a DEPLOYMENT
# placeholder: OLEKSAuthBinding (unlike ocw_studio's manual vault.Policy
# call) reads this file verbatim with no substitution step, and gwarek is
# single-environment (Production) by design — mirrors celery_monitoring's
# own policy file, which uses static paths for the same reason. Must match
# the mount_point built in __main__.py:
# f"{gwarek_db_config.engine}-gwarek-{stack_info.env_suffix}" -> engine
# defaults to "postgres" (not "postgresql") and env_suffix is "production".
path "postgres-gwarek-production/creds/app" {
  capabilities = ["read"]
}
path "postgres-gwarek-production/creds/admin" {  # pragma: allowlist secret
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
