path "database-starrocks/creds/*" {
  capabilities = ["read", "list"]
}

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
# Read-only credentials for the sql_exporter deployment, which reads the Dagster
# metadata tables directly rather than through PgBouncer. SELECT is all it needs.
path "postgres-dagster/creds/readonly/*" {
  capabilities = ["read"]
}
path "postgres-dagster/creds/readonly" {
  capabilities = ["read"]
}
# VSO's revoke_on_delete for dagster_db_secret calls sys/leases/revoke, which
# requires an explicit grant beyond the default policy's sys/leases/renew.
# Ref: https://github.com/hashicorp/vault-secrets-operator/blob/main/CHANGELOG.md
path "sys/leases/revoke" {
  capabilities = ["update"]
  allowed_parameters = {
    "lease_id" = ["postgres-dagster/creds/app/*"]
  }
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
# Keycloak identity tables, ingested by the data_loading dlt pipeline.
path "postgres-keycloak/creds/readonly/*" {
  capabilities = ["read"]
}
path "postgres-keycloak/creds/readonly" {
  capabilities = ["read"]
}
# MITx Online application database, ingested by the data_loading dlt pipeline
# (RFC 12711 step 8). Distinct from mariadb-mitxonline above, which is the
# Open edX MySQL database for the same deployment.
path "postgres-mitxonline/creds/readonly/*" {
  capabilities = ["read"]
}
path "postgres-mitxonline/creds/readonly" {
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
# X-Access-Token for the Tika service, needed by the openedx code location's
# course_document_text asset. This flat path is the canonical one: the tika
# stack writes it from the same `x_access_token` SOPS value it inlines as
# `expected` in the APISIX serverless-pre-function guarding
# tika-production.ol.mit.edu, so a token read here always matches the gateway.
# Do not point a consumer at secret-operations/{production,rc}-apps/tika/
# access-token -- tika_server_policy.hcl still grants those legacy env-scoped
# paths, but applications/tika/__main__.py stopped writing them, so a reader
# there gets a stale token and a 401 from APISIX.
path "secret-operations/tika/access-token" {
  capabilities = ["read"]
}
path "sys/leases/renew" {
  capabilities = ["update"]
}
path "secret-mitlearn/*" {
  capabilities = ["read"]
}
path "secret-mitlearn/data/*" {
  capabilities = ["read"]
}
