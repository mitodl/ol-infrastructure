path "postgres-keycloak/creds/app" {
  capabilities = ["read"]
}

path "secret-global/data/ol-wildcard" {
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
