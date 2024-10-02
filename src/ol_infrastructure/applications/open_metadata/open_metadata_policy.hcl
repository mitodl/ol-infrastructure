path "postgres-open-metadata/creds/app/*" {
  capabilities = ["read"]
}
path "postgres-open-metadata/creds/app" {
  capabilities = ["read"]
}
path "secret-operations/sso/open_metadata/*" {
  capabilities = ["read"]
}
path "secret-operations/sso/open_metadata" {
  capabilities = ["read"]
}
path "sys/leases/renew" {
  capabilities = ["update"]
}
