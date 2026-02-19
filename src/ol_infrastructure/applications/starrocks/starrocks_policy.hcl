path "secret-operations/sso/starrocks" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
