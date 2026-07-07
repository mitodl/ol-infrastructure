path "secret-operations/production-apps/tika/access-token" {
  capabilities = ["read"]
}

path "secret-operations/rc-apps/tika/access-token" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
