path "secret-operations/apisix" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
