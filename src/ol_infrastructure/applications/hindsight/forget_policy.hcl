path "secret-operations/sso/ol-memory-admin" {
  capabilities = ["read"]
}

path "secret-operations/sso/ol-memory-forget-lookup" {
  capabilities = ["read"]
}

path "DB_MOUNT/creds/bank-ids" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}

# VaultDynamicSecret revoke_on_delete.
path "sys/leases/revoke" {
  capabilities = ["update"]
  allowed_parameters = {
    "lease_id" = ["DB_MOUNT/creds/bank-ids/*"]
  }
}
