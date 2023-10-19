path "postgres-semantic/creds/app/*" {
  capabilities = ["read"]
}
path "postgres-semantic/creds/app" {
  capabilities = ["read"]
}
path "sys/leases/renew" {
  capabilities = ["update"]
}
