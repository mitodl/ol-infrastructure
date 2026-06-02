path "auth/token/lookup-self" {
  capabilities = ["read"]
}

path "sys/capabilities-self" {
  capabilities = ["read", "update"]
}

path "aws-mitx" {
  capabilities = ["read", "list"]
}

path "aws-mitx/roles" {
  capabilities = ["read", "list"]
}

path "aws-mitx/roles/eks-cluster-shared-readonly-role" {
  capabilities = ["read"]
}

path "aws-mitx/creds/eks-cluster-shared-readonly-role" {
  capabilities = ["read", "update"]
}
