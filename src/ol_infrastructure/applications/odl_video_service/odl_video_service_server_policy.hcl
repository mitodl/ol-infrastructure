path "secret-operations/*" {
  capabilities = ["read"]
}

path "secret-operations" {
  capabilities = ["read"]
}

path "postgres-odl-video-service/creds/app" {
  capabilities = ["read"]
}

path "aws-mitx/creds/ovs-server" {
  capabilities = ["read"]
}

path "secret-odl-video-service" {
  capabilities = ["read"]
}
path "secret-odl-video-service/*" {
  capabilities = ["read"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}

path "auth/token/create" {
  capabilities = ["update"]
}
