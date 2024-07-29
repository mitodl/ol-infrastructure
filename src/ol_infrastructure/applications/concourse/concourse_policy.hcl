path "postgres-concourse/creds/app_user" {
  capabilities = ["read"]
}

path "secret-concourse/*" {
  capabilities = ["read"]
}

path "secret-operations/global/odl_wildcard_cert" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
