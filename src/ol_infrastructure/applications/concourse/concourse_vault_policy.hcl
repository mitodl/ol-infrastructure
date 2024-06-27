path "*" {
  capabilities = ["read", "list"]
}

path "secret-data/*" {
  capabilities = ["read", "list"]
}


path "sys/leases/renew" {
  capabilities = ["update"]
}
