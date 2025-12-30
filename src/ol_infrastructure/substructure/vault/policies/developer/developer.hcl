path "sys/internal/ui/resultant-acl" {
    capabilities = ["read"]
}

path "auth/token/lookup-self" {
    capabilities = ["read"]
}

path "sys/capabilities-self" {
  capabilities = ["read", "update"]
}

# Read/List secret mounts to see them in UI
path "+" {
  capabilities = ["list"]
}

path "secret-*" {
  capabilities = ["read", "list"]
}

# Scratch space for developers
path "secret-sandbox/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

path "secrets-share/*" {
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

# Read Database readonly creds
path "+/creds/" {
  capabilities = ["list"]
}

path "+/creds/readonly" {
  capabilities = ["read", "list"]
}
