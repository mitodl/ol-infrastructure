path "secret-botkube/*" {
  capabilities = ["read"]
}
path "secret-botkube" {
  capabilities = ["list", "read"]
}
