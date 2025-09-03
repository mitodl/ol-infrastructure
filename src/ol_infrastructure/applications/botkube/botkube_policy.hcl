path "secret-botkube/*" {
  capabilities = ["read"]
}
path "secret-operations/botkube/*" {
  capabilities = ["list", "read"]
}
