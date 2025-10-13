path "secret-global/*" {
  capabilities = ["read", "list"]
}
path "secret-operations/sso/dagster" {
  capabilities = ["read"]
}
path "mariadb-xpro/creds/readonly/*" {
  capabilities = ["read"]
}
path "mariadb-xpro/creds/readonly" {
  capabilities = ["read"]
}
path "mariadb-xpro/*" {
  capabilities = ["read", "list"]
}
path "mariadb-xpro/" {
  capabilities = ["read", "list"]
}
path "mariadb-mitx/creds/readonly/*" {
  capabilities = ["read"]
}
path "mariadb-mitx/creds/readonly" {
  capabilities = ["read"]
}
path "mariadb-mitxonline/creds/readonly/*" {
  capabilities = ["read"]
}
path "mariadb-mitxonline/creds/readonly" {
  capabilities = ["read"]
}
path "postgres-dagster/creds/app/*" {
  capabilities = ["read"]
}
path "postgres-dagster/creds/app" {
  capabilities = ["read"]
}
path "postgres-dagster-data-production/creds/app/*" {
  capabilities = ["read"]
}
path "postgres-dagster-data-production/creds/app" {
  capabilities = ["read"]
}
path "postgresql-micromasters/creds/readonly/*" {
  capabilities = ["read"]
}
path "postgresql-micromasters/creds/readonly" {
  capabilities = ["read"]
}
path "postgres-micromasters/creds/readonly/*" {
  capabilities = ["read"]
}
path "postgres-micromasters/creds/readonly" {
  capabilities = ["read"]
}
path "secret-data/" {
  capabilities = ["list"]
}
path "secret-data/dagster/*" {
  capabilities = ["read"]
}
path "secret-data/dagster" {
  capabilities = ["read"]
}
path "secret-data/pipelines/*" {
  capabilities = ["read"]
}
path "secret-data/pipelines" {
  capabilities = ["read"]
}
path "secret-data/superset_service_account" {
  capabilities = ["read"]
}
path "secret-data/dagster-http-auth-password" {
  capabilities = ["read"]
}
path "secret-data/dagster-dbt-creds" {
  capabilities = ["read"]
}
path "secret-mitx/mongodb-forum/*" {
  capabilities = ["read"]
}
path "secret-mitx/mongodb-forum" {
  capabilities = ["read"]
}
path "secret-mitxonline/mongodb-forum/*" {
  capabilities = ["read"]
}
path "secret-mitxonline/mongodb-forum" {
  capabilities = ["read"]
}
path "secret-xpro/mongodb-forum/*" {
  capabilities = ["read"]
}
path "secret-xpro/mongodb-forum" {
  capabilities = ["read"]
}
path "secret-operations/data/institutional-research-bigquery-service-account" {
  capabilities = ["read"]
}
path "sys/leases/renew" {
  capabilities = ["update"]
}
