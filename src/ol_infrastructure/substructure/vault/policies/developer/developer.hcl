path "sys/internal/ui/resultant-acl" {
    capabilities = ["read"]
}

path "auth/token/lookup-self" {
    capabilities = ["read"]
}

path "sys/capabilities-self" {
  capabilities = ["read", "update"]
}

# List secret mounts to see them in UI
path "secret-*" {
  capabilities = ["list"]
}

# Read/List all secrets at secret-* mounts
path "secret-*/*" {
  capabilities = ["read", "list"]
}

# Add details to support KVv2
path "secret-*/metadata/" {
  capabilities = ["list", "read"]
}

path "secret-*/data/*" {
  capabilities = ["list", "read"]
}

# Scratch space for developers
path "secret-sandbox/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

# To allow Devs to update creds and
# run the replication on their own
path "secret-concourse/ocw/ocw-studio-db-replication" {
  capabilities = ["read", "list", "update"]
}

# Read AWS iAM creds
path "aws-mitx/*" {
  capabilities = ["read", "list"]
}

# Read MariaDB readonly creds
path "mariadb-*" {
  capabilities = ["list"]
}

path "mariadb-*/creds/readonly" {
  capabilities = ["read"]
}

# Generate Postgres readonly creds
path "postgres-*" {
  capabilities = ["list"]
}

path "postgres-*/creds/readonly" {
  capabilities = ["read"]
}
