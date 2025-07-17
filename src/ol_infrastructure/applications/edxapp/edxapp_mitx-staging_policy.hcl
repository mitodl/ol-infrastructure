path "mariadb-mitx-staging/creds/edxapp" {
  capabilities = ["read"]
}

path "mariadb-mitx-staging/creds/edxapp-csmh" {
  capabilities = ["read"]
}

path "mariadb-mitx-staging/creds/xqueue" {
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

path "secret-mitx-staging/mitx-staging-wildcard-certificate" {
  capabilities = ["read"]
}

path "secret-global/learn_ai" {
  capabilities = ["read"]
}

path "secret-global/data/learn_ai" {
  capabilities = ["read"]
}
