path "secret-celery-monitoring/*" {
  capabilities = ["read", "list"]
}

path "secret-celery-monitoring/data/*" {
  capabilities = ["read", "list"]
}

path "secret-operations/sso/leek" {
  capabilities = ["read"]
}
