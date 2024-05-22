path "sys/leases/renew" {
  capabilities = [ "update" ]
}

path "secret-xqwatcher/ENV_PREFIX-grader-static-secrets/*" {
  capabilities = [ "read" ]
}
