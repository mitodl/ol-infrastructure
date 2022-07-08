path "secret-operations/global/odl_wildcard_cert" {
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
path "mariadb-xpro" {
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

path "postgres-dagster-data-qa/creds/app/*" {
  capabilities = ["read"]
}
path "postgres-dagster-data-qa/creds/app" {
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

path "postgresql-micromasters/*" {
  capabilities = ["read"]
}
path "postgresql-micromasters" {
  capabilities = ["read"]
}
path "postgres-micromasters/*" {
  capabilities = ["read"]
}
path "postgres-micromasters" {
  capabilities = ["read"]
}
path "postgres-rc-apps-opendiscussions/creds/readonly/*" {
  capabilities = ["read"]
}
path "postgres-rc-apps-opendiscussions/creds/readonly" {
  capabilities = ["read"]
}
path "postgres-rc-apps-opendiscussions/creds/opendiscussions/*" {
  capabilities = ["read"]
}
path "postgres-rc-apps-opendiscussions/creds/opendiscussions" {
  capabilities = ["read"]
}

path "secret-data/*" {
  capabilities = ["read"]
}
path "secret-data" {
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
path "secret-operations/global/odl_wildcard_cert/*" {
  capabilities = ["read"]
}
path "secret-operations/global/odl_wildcard_cert" {
  capabilities = ["read"]
}
path "secret-operations/data/institutional-research-bigquery-service-account/*" {
  capabilities = ["read"]
}
path "secret-operations/data/institutional-research-bigquery-service-account" {
  capabilities = ["read"]
}

path "secret-data/data-qa/pipelines/edx/residential/healthchecks-io.check-id/*" {
  capabilities = ["read"]
}
path "secret-data/data-qa/pipelines/edx/residential/healthchecks-io.check-id" {
  capabilities = ["read"]
}
path "secret-data/data-production/pipelines/edx/residential/healthchecks-io.check-id/*" {
  capabilities = ["read"]
}
path "secret-data/data-production/pipelines/edx/residential/healthchecks-io.check-id" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
