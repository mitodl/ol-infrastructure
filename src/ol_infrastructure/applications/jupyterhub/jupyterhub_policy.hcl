path "secret-operations/sso/jupyterhub/*" {
  capabilities = ["read"]
}
path "secret-operations/sso/jupyterhub" {
  capabilities = ["read"]
}

path "postgres-jupyterhub/creds/app" {
  capabilities = ["read"]
}

# vault-secrets-operator is a little more particular about
# managing its own leases, give it the permissions it needs
# for dynamic secret renwals / revocation without giving
# it the power to revoke or renew anything
path "sys/leases/renew" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-jupyterhub/creds/app/*"]
  }
}
path "sys/leases/revoke" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-jupyterhub/creds/app/*"]
  }
}
