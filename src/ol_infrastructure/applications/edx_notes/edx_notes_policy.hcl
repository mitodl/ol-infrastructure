path "sys/leases/renew" {
  capabilities = [ "update" ]
}
path "secret-DEPLOYMENT/edx-notes" {
  capabilities = ["read"]
}
path "secret-DEPLOYMENT/edx-notes/*" {
  capabilities = ["read"]
}
path "mariadb-DEPLOYMENT/creds/notes" {
  capabilities = ["read"]
}
