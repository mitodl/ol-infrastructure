path "sys/leases/renew" {
  capabilities = [ "update" ]
}

path "secret-xqwatcher/*" {
  capabilities = [ "read" ]
}

path "secret-DEPLOYMENT/edx-xqueue" {
  capabilities = [ "read" ]
}
