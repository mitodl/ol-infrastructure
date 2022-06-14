path "secret-DEPLOYMENT/mongodb-forum/*" {
  capabilities = ["read"]
}

path "secret-DEPLOYMENT/edxapp-forum" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = [ "update" ]
}
