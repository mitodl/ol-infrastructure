path "secret-operations/sso/ol-memory-admin" {
  capabilities = ["read"]
}

path "secret-operations/sso/ol-memory-forget-lookup" {
  capabilities = ["read"]
}

path "DB_MOUNT/creds/bank-ids" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
