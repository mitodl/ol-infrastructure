path "secret-superset/*" {
  capabilities = ["read"]
}

path "secret-operations/sso/superset" {
  cpabilities = ["read"]
}

path "postgres-superset/creds/app" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
