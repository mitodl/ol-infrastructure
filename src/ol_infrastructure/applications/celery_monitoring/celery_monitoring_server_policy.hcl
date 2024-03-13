path "secret-celery-monitoring/*" {
  capabilities = ["read", "list"]
}

path "secret-celery-monitoring/data/*" {
  capabilities = ["read", "list"]
}

path "secret-operations/global/odl_wildcard_cert" {
  capabilities = ["read"]
}

path "secret-operations/sso/leek" {
  capabilities = ["read"]
}
