path "postgres-keycloak/creds/app" {
  capabilities = ["read"]
}

path "secret-keycloak" {
  capabilities = ["read"]
}
path "secret-keycloak/*" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
