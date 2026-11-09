path "secret-operations/sso/starrocks" {
  capabilities = ["read"]
}

# The starrocks-group-sync CronJob's StarRocks user (substructure/starrocks,
# group_sync_job.py). Holds SECURITY on SYSTEM.
path "database-starrocks/creds/group_provider_admin" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}

# VaultDynamicSecret revoke_on_delete.
path "sys/leases/revoke" {
  capabilities = ["update"]
  allowed_parameters = {
    "lease_id" = ["database-starrocks/creds/group_provider_admin/*"]
  }
}
