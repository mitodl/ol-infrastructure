path "mariadb-mitx/creds/edxapp" {
  capabilities = ["read"]
}

path "mariadb-mitx/creds/edxapp-csmh" {
  capabilities = ["read"]
}

path "mariadb-mitx/creds/xqueue" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = [ "update" ]
}

path "secret-mitx/edxapp" {
  capabilities = ["read"]
}

path "secret-mitx/edx-forum" {
  capabilities = ["read"]
}

path "secret-mitx/edx-xqueue" {
  capabilities = ["read"]
}

path "secret-mitx/mongodb-edxapp" {
  capabilities = ["read"]
}

path "secret-mitx/mongodb-forum" {
  capabilities = ["read"]
}

path "secret-operations/global/github-enterprise-ssh" {
  capabilities = ["read"]
}

path "secret-mitx/mitx-wildcard-certificate" {
  capabilities = ["read"]
}
