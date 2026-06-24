path "secret-clickhouse/data/credentials" {
  capabilities = ["read"]
}

path "secret-clickhouse/metadata/credentials" {
  capabilities = ["read", "list"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
