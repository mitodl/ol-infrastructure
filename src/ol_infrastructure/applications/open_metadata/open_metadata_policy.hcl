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
path "secret-openmetadata/data/keycloak-team-sync" {
  capabilities = ["read"]
}
# The provisioner's Keycloak client, an OpenMetadata admin, for the same job's
# role reconciliation
path "secret-openmetadata/data/pulumi-provisioner" {
  capabilities = ["read"]
}
# Connector credentials for OpenMetadata ingestion pipelines
path "secret-openmetadata/data/connectors" {
  capabilities = ["read"]
}
path "secret-openmetadata/data/connectors/*" {
  capabilities = ["read"]
}
# System bot JWTs for authenticating to the OpenMetadata server API, written by
# substructure/open_metadata. One path per bot the ingestion jobs use.
path "secret-openmetadata/data/ingestion-bot" {
  capabilities = ["read"]
}
path "secret-openmetadata/data/lineage-bot" {
  capabilities = ["read"]
}
path "secret-openmetadata/data/profiler-bot" {
  capabilities = ["read"]
}
# Read-only StarRocks credentials for the StarRocks ingestion jobs. Only QA
# and Production create the secret that reads this path. batch_readonly holds
# readonly's grants and puts the jobs' queries in the batch resource group.
path "database-starrocks/creds/batch_readonly" {
  capabilities = ["read"]
}
path "database-starrocks/creds/batch_readonly/*" {
  capabilities = ["read"]
}
# vault-secrets-operator is a little more particular about
# managing its own leases, give it the permissions it needs
# for dynamic secret renewals / revocation without giving
# it the power to revoke or renew anything
path "sys/leases/renew" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-open-metadata/creds/app/*", "database-starrocks/creds/batch_readonly/*"]
  }
}
path "sys/leases/revoke" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-open-metadata/creds/app/*", "database-starrocks/creds/batch_readonly/*"]
  }
}
