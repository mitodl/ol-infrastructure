path "secret-observability-archive/*" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
