path "sys/leases/renew" {
  capabilities = [ "update" ]
}

path "secret-DEPLOYMENT/edx-xqueue" {
  capabilities = [ "read" ]
}

path "secret-DEPLOYMENT/edxorg-xqueue" {
  capabilities = [ "read" ]
}
