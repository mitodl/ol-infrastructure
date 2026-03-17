path "aws-mitx/creds/micromasters" {
  capabilities = ["read"]
}
path "aws-mitx/creds/micromasters/*" {
  capabilities = ["read"]
}

path "postgres-micromasters/creds/app" {
  capabilities = ["read"]
}
path "postgres-micromasters/creds/app/*" {
  capabilities = ["read"]
}

path "secret-micromasters" {
  capabilities = ["read"]
}
path "secret-micromasters/*" {
  capabilities = ["read"]
}

path "secret-operations/mailgun" {
  capabilities = ["read"]
}

path "secret-operations/mit-smtp" {
  capabilities = ["read"]
}

# vault-secrets-operator is a little more particular about
# managing its own leases, give it the permissions it needs
# for dynamic secret renewals / revocation without giving
# it the power to revoke or renew anything
path "sys/leases/renew" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-micromasters/creds/app/*", "aws-mitx/creds/micromasters/*"]
  }
}
path "sys/leases/revoke" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-micromasters/creds/app/*", "aws-mitx/creds/micromasters/*"]
  }
}
