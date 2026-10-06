path "postgres-gravitino/creds/app" {
  capabilities = ["read"]
}

path "secret-operations/sso/gravitino-admin" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
