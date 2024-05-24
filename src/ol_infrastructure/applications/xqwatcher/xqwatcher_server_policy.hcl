path "sys/leases/renew" {
  capabilities = [ "update" ]
}

path "secret-xqwatcher/*" {
  capabilities = [ "read" ]
}
