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

path "secret-mitxonline/mongodb-edxapp" {
  capabilities = ["read"]
}

path "secret-mitxonline/mongodb-forum" {
  capabilities = ["read"]
}

path "secret-mitxonline/edx-xqueue" {
  capabilities = ["read"]
}

path "secret-operations/global/github-enterprise-ssh" {
  capabilities = ["read"]
}

path "secret-mitxonline/mitxonline-wildcard-certificate" {
  capabilities = ["read"]
}

path "secret-global/learn_ai" {
  capabilities = ["read"]
}

path "secret-global/data/learn_ai" {
  capabilities = ["read"]
}

path "secret-global/data/grafana" {
  capabilities = ["read"]
}
