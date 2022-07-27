# Github MITODL org developer permissions on QA environment

# Read app secrets
path "secret-bootcamps/*"
{
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

path "secret/mitodl/*" {
  capabilities = ["read", "write", "update", "delete", "list"]
}

# Read AWS iAM creds
path "aws-mitx/mit-open-application-*" {
  capabilities = ["read"]
}

path "aws-mitx/mitxonline*" {
  capabilities = ["read"]
}

path "aws-mitx/ocw-studio-app-*" {
  capabilities = ["read"]
}

path "aws-mitx/odl-video-service-*" {
  capabilities = ["read"]
}

path "aws-mitx/read-write-delete-ol-bootcamps-app-*" {
  capabilities = ["read"]
}

path "aws-mitx/read-write-delete-xpro-app-*" {
  capabilities = ["read"]
}
