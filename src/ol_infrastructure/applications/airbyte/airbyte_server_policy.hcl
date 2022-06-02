path "secret-operations/global/odl_wildcard_cert" {
  capabilities = ["read"]
}

path "postgres-airbyte/creds/app" {
  capabilities = ["read"]
}

path "postgres-airbyte/creds/admin" {
  capabilities = ["read"]
}

path "secret-airbyte/" {
  capabilities = ["read"]
}

path "secret-airbyte/*" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
