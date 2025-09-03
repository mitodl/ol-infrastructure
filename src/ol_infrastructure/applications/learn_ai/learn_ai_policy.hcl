path "postgres-learn-ai/creds/app/*" {
  capabilities = ["read"]
}
path "postgres-learn-ai/creds/app" {
  capabilities = ["read"]
}

path "secret-operations/sso/learn-ai" {
  capabilities = ["read"]
}
path "secret-operations/sso/mitlearn" {
  capabilities = ["read"]
}

path "secret-learn-ai/*" {
  capabilities = ["read"]
}
path "secret-learn-ai" {
  capabilities = ["read"]
}
# vault-secrets-operator is a little more particular about
# managing its own leases, give it the permissions it needs
# for dynamic secret renwals / revocation without giving
# it the power to revoke or renew anything
path "sys/leases/renew" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-learn-ai/creds/app/*"]
  }
}
path "sys/leases/revoke" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-learn-ai/creds/app/*"]
  }
}

path "secret-global/learn_ai" {
  capabilities = ["read"]
}
