path "secret-DEPLOYMENT/mongodb-forum" {
  capabilities = ["read"]
}

path "secret-DEPLOYMENT/edx-forum" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = [ "update" ]
}
