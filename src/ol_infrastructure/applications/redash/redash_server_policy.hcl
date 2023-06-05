path "secret-operations/global/odl_wildcard_cert" {
  capabilities = ["read"]
}

path "secret-data/redash/*" {
  capabilities = ["read"]
}
path "secret-data/redash" {
  capabilities = ["read"]
}

path "postgres-redash/creds/app" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}

path "postgres-mitxonline/creds/readonly" {
  capabilities = ["read"]
}

path "postgres-micromasters/creds/readonly" {
  capabilities = ["read"]
}

path "postgres-bootcamps/creds/readonly" {
  capabilities = ["read"]
}

path "postgres-mitxonline/creds/readonly/*" {
  capabilities = ["read"]
}

path "postgres-production-apps-reddit/creds/readonly" {
  capabilities = ["read"]
}
path "postgres-production-apps-reddit/creds/readonly/*" {
  capabilities = ["read"]
}

path "postgres-production-apps-opendiscussions/creds/readonly" {
  capabilities = ["read"]
}
path "postgres-production-apps-opendiscussions/creds/readonly/*" {
  capabilities = ["read"]
}

path "postgres-odl-video-service/creds/readonly" {
  capabilities = ["read"]
}
path "postgres-odl-video-service/creds/readonly/*" {
  capabilities = ["read"]
}

path "postgres-ocw-studio-applications-production/creds/readonly" {
  capabilities = ["read"]
}
path "postgres-ocw-studio-applications-production/creds/readonly/*" {
  capabilities = ["read"]
}

path "postgres-production-apps-mitxpro/creds/readonly" {
  capabilities = ["read"]
}
path "postgres-production-apps-mitxpro/creds/readonly/*" {
  capabilities = ["read"]
}
