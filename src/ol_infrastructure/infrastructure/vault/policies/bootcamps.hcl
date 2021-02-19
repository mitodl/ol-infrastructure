# bootcamps-default-policy
path "secret-bootcamps/*"
{
  capabilities = ["read"]
}

path "secret-operations/global/*" {
  capabilities = ["read"]
}

path "postgresql-bootcamps/creds/app" {
  capabilities = ["read"]
}
