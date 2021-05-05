path "secret-concourse/*" {
  capabilities = ["read"]
}

path "postgres-concourse/creds/app" {
  capabilities = ["read"]
}
