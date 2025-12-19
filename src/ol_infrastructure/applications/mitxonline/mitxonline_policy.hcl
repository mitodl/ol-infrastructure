path "aws-mitx/creds/mitxonline" {
  capabilities = ["read"]
}
path "aws-mitx/creds/mitxonline/*" {
  capabilities = ["read"]
}

path "postgres-mitxonline/creds/app" {
  capabilities = ["read"]
}
path "postgres-mitxonline/creds/app/*" {
  capabilities = ["read"]
}

path "secret-operations/global/mitxonline/sentry-dsn" {
  capabilities = ["read"]
}
path "secret-global/data/mailgun" {
  capabilities = ["read"]
}

path "secret-mitxonline" {
  capabilities = ["read"]
}
path "secret-mitxonline/*" {
  capabilities = ["read"]
}

# MITXOnline needs access to the DID and bearer tokens for authentication with Digital Credentials services
path "secret-digital-credentials/data/issuer-coordinator" {
  capabilities = ["read"]
}
# vault-secrets-operator is a little more particular about
# managing its own leases, give it the permissions it needs
# for dynamic secret renwals / revocation without giving
# it the power to revoke or renew anything
path "sys/leases/renew" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-mitopen/creds/app/*"]
  }
}
path "sys/leases/revoke" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-mitopen/creds/app/*"]
  }
}

path "secret-operations/sso/mitlearn" {
  capabilities = ["read"]
}
