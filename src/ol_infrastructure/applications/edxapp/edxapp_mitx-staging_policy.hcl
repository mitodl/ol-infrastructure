path "mariadb-mitx-staging/creds/edxapp" {
  capabilities = ["read"]
}

path "mariadb-mitx-staging/creds/edxapp-csmh" {
  capabilities = ["read"]
}

path "mongodb-mitx-staging/creds/edxapp" {
  capabilities = ["read"]
}

path "mongodb-mitx-staging/creds/forum" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = [ "update" ]
}

path "secret-mitx-staging/edxapp" {
  capabilities = ["read"]
}

path "secret-mitx-staging/edx-forum" {
  capabilities = ["read"]
}

path "secret-mitx-staging/edx-xqueue" {
  capabilities = ["read"]
}

path "secret-mitx-staging/mongodb-edxapp" {
  capabilities = ["read"]
}

path "secret-mitx-staging/mongodb-forum" {
  capabilities = ["read"]
}

path "secret-operations/global/github-enterprise-ssh" {
  capabilities = ["read"]
}

path "secret-mitx-staging/mitx-wildcard-certificate" {
  capabilities = ["read"]
}
