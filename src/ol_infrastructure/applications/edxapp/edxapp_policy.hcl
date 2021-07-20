path "mariadb-mitxonline/creds/edxapp" {
  capabilities = ["read"]
}

path "mariadb-mitxonline/creds/edxapp-csmh" {
  capabilities = ["read"]
}

path "mongodb-mitxonline/creds/edxapp" {
  capabilities = ["read"]
}

path "mongodb-mitxonline/creds/forum" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = [ "update" ]
}

path "secret-mitxonline/edxapp" {
  capabilities = ["read"]
}

path "secret-mitxonline/edx-forum" {
  capabilities = ["read"]
}

path "secret-mitxonline/edx-xqueue" {
  capabilities = ["read"]
}

path "secret-mitxonline/mitxonline-wildcard-certificate" {
  capabilities = ["read"]
}
