path "mariadb-mitxonline/creds/notes" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = [ "update" ]
}

path "secret-mitxonline/edx-notes-api" {
  capabilities = ["read"]
}
