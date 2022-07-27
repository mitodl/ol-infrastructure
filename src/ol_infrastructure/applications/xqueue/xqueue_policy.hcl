path "mariadb-DEPLOYMENT/creds/xqueue" {
  capabilities = ["read"]
}

path "secret-DEPLOYMENT/edx-xqueue" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = [ "update" ]
}
