path "secret-operations/*" {
  capabilities = ["read"]
}
path "secret-operations" {
  capabilities = ["read"]
}

path "postgres-keycloak/creds/app" {
  capabilities = ["read"]
}

path "secret-keycloak" {
  capabilities = ["read"]
}
path "secret-keycloak/*" {
  capabilities = ["read"]
}
