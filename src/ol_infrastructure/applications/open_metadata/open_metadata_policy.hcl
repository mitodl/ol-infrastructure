path "postgres-open-metadata/creds/app/*" {
  capabilities = ["read"]
}
path "postgres-open-metadata/creds/app" {
  capabilities = ["read"]
}
path "secret-operations/sso/open_metadata/*" {
  capabilities = ["read"]
}
path "secret-operations/sso/open_metadata" {
  capabilities = ["read"]
}
# Superset's OIDC client, used by the Superset ingestion job
path "secret-operations/sso/superset" {
  capabilities = ["read"]
}
# Keycloak client for the job that mirrors team membership into Keycloak groups
path "secret-operations/sso/openmetadata-team-sync" {
  capabilities = ["read"]
}
# Connector credentials for OpenMetadata ingestion pipelines
path "secret-openmetadata/data/connectors" {
  capabilities = ["read"]
}
path "secret-openmetadata/data/connectors/*" {
  capabilities = ["read"]
}
# Ingestion-bot JWT tokens for authenticating to the OpenMetadata server API.
# One path per system bot (ingestion, lineage, profiler, data-insight).
path "secret-openmetadata/data/ingestion-bot" {
  capabilities = ["read"]
}
path "secret-openmetadata/data/lineage-bot" {
  capabilities = ["read"]
}
path "secret-openmetadata/data/profiler-bot" {
  capabilities = ["read"]
}
path "secret-openmetadata/data/data-insight-bot" {
  capabilities = ["read"]
}
# Read-only StarRocks credentials for the StarRocks ingestion jobs. Only QA
# and Production create the secret that reads this path.
path "database-starrocks/creds/readonly" {
  capabilities = ["read"]
}
path "database-starrocks/creds/readonly/*" {
  capabilities = ["read"]
}
# vault-secrets-operator is a little more particular about
# managing its own leases, give it the permissions it needs
# for dynamic secret renewals / revocation without giving
# it the power to revoke or renew anything
path "sys/leases/renew" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-open-metadata/creds/app/*", "database-starrocks/creds/readonly/*"]
  }
}
path "sys/leases/revoke" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-open-metadata/creds/app/*", "database-starrocks/creds/readonly/*"]
  }
}
