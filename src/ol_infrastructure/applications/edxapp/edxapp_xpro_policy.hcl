path "mariadb-xpro/creds/edxapp" {
  capabilities = ["read"]
}

path "mariadb-xpro/creds/edxapp-csmh" {
  capabilities = ["read"]
}

path "mongodb-xpro/creds/edxapp" {
  capabilities = ["read"]
}

path "mongodb-xpro/creds/forum" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = [ "update" ]
}

path "secret-xpro/edxapp" {
  capabilities = ["read"]
}

path "secret-xpro/edx-forum" {
  capabilities = ["read"]
}

path "secret-xpro/edx-xqueue" {
  capabilities = ["read"]
}

path "secret-operations/global/github-enterprise-ssh" {
  capabilities = ["read"]
}

path "secret-xpro/xpro-wildcard-certificate" {
  capabilities = ["read"]
}
