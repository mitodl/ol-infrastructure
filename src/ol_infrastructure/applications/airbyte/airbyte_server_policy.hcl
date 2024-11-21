path "secret-operations/global/odl_wildcard_cert" {
  capabilities = ["read"]
}

path "postgres-airbyte/creds/app" {
  capabilities = ["read"]
}

path "postgres-airbyte/creds/admin" {
  capabilities = ["read"]
}

path "secret-airbyte/" {
  capabilities = ["read", "create", "patch", "delete", "list", "update"]
}

path "secret-airbyte/*" {
  capabilities = ["read", "create", "patch", "delete", "list", "update"]
}

path "secret-operations/sso/airbyte" {
  capabilities = ["read"]
}

path "auth/token/create" {
  capabilities = ["update"]
}
path "sys/leases/renew" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-airbyte/creds/app/*", "postgres-airbyte/creds/admin/*"]
  }
}
path "sys/leases/revoke" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-airbyte/creds/app/*", "postgres-airbyte/creds/admin/*"]
  }
}
