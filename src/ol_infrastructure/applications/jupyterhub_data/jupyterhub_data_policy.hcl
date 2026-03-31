# Read OIDC client credentials for JupyterHub GenericOAuthenticator
path "secret-operations/data/sso/marimo" {
  capabilities = ["read"]
}
path "secret-operations/sso/marimo" {
  capabilities = ["read"]
}

# Read service-account Trino credentials for published apps (marimo-operator)
path "secret-operations/data/sso/marimo-app" {
  capabilities = ["read"]
}
path "secret-operations/sso/marimo-app" {
  capabilities = ["read"]
}

# Read JUPYTERHUB_CRYPT_KEY for auth state encryption
path "secret-operations/data/jupyterhub-data/crypt-key" {
  capabilities = ["read"]
}
path "secret-operations/jupyterhub-data/crypt-key" {
  capabilities = ["read"]
}

path "postgres-jupyterhub-data/creds/app" {
  capabilities = ["read"]
}

# vault-secrets-operator lease management for dynamic database credentials
path "sys/leases/renew" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-jupyterhub-data/creds/app/*"]
  }
}
path "sys/leases/revoke" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-jupyterhub-data/creds/app/*"]
  }
}
