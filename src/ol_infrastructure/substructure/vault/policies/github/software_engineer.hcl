# Github MITODL org developer permissions on QA environment

path "sys/capabilities-self" {
  capabilities = ["read", "update"]
}

# List
path "secret-*/*" {
  capabilities = ["list"]
}

# Read Concourse secrets
path "secret-concourse/*" {
  capabilities = ["read", "list"]
}

# Read app secrets
path "secret-bootcamps/*" {
  capabilities = ["read", "list"]
}

path "secret-micromasters/*" {
  capabilities = ["read", "list"]
}

path "secret-mit-open/*" {
  capabilities = ["read", "list"]
}

path "secret-mitxonline/*" {
  capabilities = ["read", "list"]
}

path "secret-mitxpro/*" {
  capabilities = ["read", "list"]
}

path "secret-ocw-studio/*" {
  capabilities = ["read", "list"]
}

path "secret-odl-video/*" {
  capabilities = ["read", "list"]
}

# Read/Write/Delete/List generic secrets
# Mainly a safe way to share secrets with others

path "secret/*" {
  capabilities = ["list"]
}

path "secret/mitodl/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

# Read AWS iAM creds
path "aws-mitx/*" {
  capabilities = ["list"]
}

path "aws-mitx/creds/mit-open-application-*" {
  capabilities = ["read", "list"]
}

path "aws-mitx/creds/mitxonline*" {
  capabilities = ["read", "list"]
}

path "aws-mitx/creds/ocw-studio-app-*" {
  capabilities = ["read", "list"]
}

path "aws-mitx/creds/odl-video-service-*" {
  capabilities = ["read", "list"]
}

path "aws-mitx/creds/read-write-delete-ol-bootcamps-app-*" {
  capabilities = ["read", "list"]
}

path "aws-mitx/creds/read-write-delete-xpro-app-*" {
  capabilities = ["read", "list"]
}
