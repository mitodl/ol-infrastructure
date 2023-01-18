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
path "postgres-mitxonline/creds/readonly/*" {
  capabilities = ["read"]
}
