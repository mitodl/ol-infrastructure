path "postgres-ecommerce/creds/app/*" {
  capabilities = ["read"]
}
path "postgres-ecommerce/creds/app" {
  capabilities = ["read"]
}

path "secret-operations/sso/unified-ecommerce/*" {
  capabilities = ["read"]
}
path "secret-operations/sso/unified-ecommerce" {
  capabilities = ["read"]
}

path "secret-ecommerce/*" {
  capabilities = ["read"]
}
path "secret-ecommerce" {
  capabilities = ["read"]
}
# vault-secrets-operator is a little more particular about
# managing its own leases, give it the permissions it needs
# for dynamic secret renwals / revocation without giving
# it the power to revoke or renew anything
path "sys/leases/renew" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-ecommerce/creds/app/*"]
  }
}
path "sys/leases/revoke" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-ecommerce/creds/app/*"]
  }
}
