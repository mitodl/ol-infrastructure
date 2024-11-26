path "secret-dev/*" {
  capabilities = ["list", "read"]
}

path "secret-operations/*" {
    capabilities = ["list"]
}

path "secret-operations/global/*" {
    capabilities = ["list", "read"]
}

path "transit/*" {
    capabilities = ["create", "read", "update", "delete", "list"]
}
