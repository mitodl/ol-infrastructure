path "secret-lightdash/*" {
  capabilities = ["read"]
}

path "secret-lightdash/data/*" {
  capabilities = ["read"]
}

path "secret-operations/sso/lightdash" {
  capabilities = ["read"]
}

path "postgres-lightdash/creds/app" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
