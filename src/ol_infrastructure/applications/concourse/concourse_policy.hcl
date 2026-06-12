path "postgres-concourse/creds/app" {
  capabilities = ["read"]
}

path "secret-concourse/*" {
  capabilities = ["read"]
}

path "secret-operations/sso/concourse" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
