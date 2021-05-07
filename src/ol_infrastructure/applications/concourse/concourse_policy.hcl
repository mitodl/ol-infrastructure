path "secret-concourse/*" {
  capabilities = ["read"]
}

path "postgres-concourse/creds/app" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
