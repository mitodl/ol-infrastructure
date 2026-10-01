path "postgres-gravitino/creds/app" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
