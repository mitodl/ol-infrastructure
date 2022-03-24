path "secret-vector-log-proxy/*" {
  capabilities = ["read"]
}

path "secret-operations/global/odl_wildcard_cert" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
