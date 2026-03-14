path "secret-clickhouse/*" {
  capabilities = ["read"]
}

path "secret-clickhouse/data/*" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}
