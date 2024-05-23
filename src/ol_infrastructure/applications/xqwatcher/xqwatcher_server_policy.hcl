path "sys/leases/renew" {
  capabilities = [ "update" ]
}

path "secret-xqwatcher/ENV_PREFIX-grader-config" {  # pragma: allowlist secret
  capabilities = [ "read" ]
}
