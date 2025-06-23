path "aws-mitx/creds/ol-mitlearn-application" {
  capabilities = ["read"]
}
path "aws-mitx/creds/ol-mitlearn-application/*" {
  capabilities = ["read"]
}

path "postgres-mitlearn/creds/app" {
  capabilities = ["read"]
}
path "postgres-mitlearn/creds/app/*" {
  capabilities = ["read"]
}

path "secret-operations/sso/mitlearn/*" {
  capabilities = ["read"]
}
path "secret-operations/sso/mitlearn" {
  capabilities = ["read"]
}

path "secret-operations/global/embedly" {
  capabilities = ["read"]
}
path "secret-operations/global/embedly/*" {
  capabilities = ["read"]
}
path "secret-operations/global/odlbot-github-access-token" {
  capabilities = ["read"]
}
path "secret-operations/global/mit-smtp" {
  capabilities = ["read"]
}
path "secret-operations/global/update-search-data-webhook-key" {
  capabilities = ["read"]
}
path "secret-operations/tika/access-token" {
  capabilities = ["read"]
}
path "secret-global/data/mailgun" {
  capabilities = ["read"]
}
path "secret-global/data/shared_hmac" {
  capabilities = ["read"]
}

path "secret-mitlearn/*" {
  capabilities = ["read"]
}
# vault-secrets-operator is a little more particular about
# managing its own leases, give it the permissions it needs
# for dynamic secret renwals / revocation without giving
# it the power to revoke or renew anything
path "sys/leases/renew" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-mitlearn/creds/app/*"]
  }
}
path "sys/leases/revoke" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-mitlearn/creds/app/*"]
  }
}
