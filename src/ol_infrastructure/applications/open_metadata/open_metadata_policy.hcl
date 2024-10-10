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
# vault-secrets-operator is a little more particular about
# managing its own leases, give it the permissions it needs
# for dynamic secret renwals / revocation without giving
# it the power to revoke or renew anything
path "sys/leases/renew" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-open-metadata/creds/app/*"]
  }
}
path "sys/leases/renew" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-open-metadata/creds/app/*"]
  }
}
