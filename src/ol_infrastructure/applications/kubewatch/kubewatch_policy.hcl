path "secret-kubewatch/*" {
  capabilities = ["read"]
}
path "secret-operations/kubewatch/*" {
  capabilities = ["list", "read"]
}
