path "secret-operations/sso/jupyterhub/*" {
  capabilities = ["read"]
}
path "secret-operations/sso/jupyterhub" {
  capabilities = ["read"]
}

path "postgres-jupyterhub/creds/app" {
  capabilities = ["read"]
}
