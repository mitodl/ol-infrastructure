# Vault policy for the ol-analytics-api k8s service account (Kubernetes auth
# role "ol-analytics-api").  This single role is bound to two service accounts
# (see __main__.py):
#   * ol-analytics-api        - the app pod itself, which does a direct hvac
#                               Kubernetes login and reads its short-lived
#                               StarRocks credentials from
#                               database-starrocks/creds/app.
#   * ol-analytics-api-vault  - the vault-secrets-operator ServiceAccount that
#                               syncs the APISIX OIDC client secret and the
#                               static application secrets (SENTRY_DSN) into
#                               Kubernetes Secrets.
#
# The StarRocks dynamic-credentials path is appended to this base policy at
# deploy time (see __main__.py), matching the pattern used by
# superset_server_policy.hcl. The mount name itself is fixed (not
# environment-specific) since QA and Production each run their own, entirely
# separate Vault deployment.

# APISIX OIDC (Keycloak) client config consumed by OLApisixOIDCResources.
path "secret-operations/sso/ol-analytics-api" {
  capabilities = ["read"]
}

# Static application secrets (SENTRY_DSN, ...) synced via the
# vault-secrets-operator into the ol-analytics-api-static-secrets K8s Secret.
# secret-ol-analytics-api is a kv-v2 mount, so reads go through the /data/
# sub-path the KV-v2 backend exposes (see superset_server_policy.hcl) -- the
# bare path below does not cover it.
path "secret-ol-analytics-api/*" {
  capabilities = ["read"]
}
path "secret-ol-analytics-api/data/*" {
  capabilities = ["read"]
}
path "secret-ol-analytics-api" {
  capabilities = ["read"]
}
