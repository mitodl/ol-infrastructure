path "secret-superset/*" {
  capabilities = ["read"]
}

path "secret-operations/sso/superset" {
  capabilities = ["read"]
}

path "postgres-superset/creds/app" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
