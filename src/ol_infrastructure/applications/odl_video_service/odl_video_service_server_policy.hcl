path "secret-operations/global/cloudfront-private-key" {
  capabilities = ["read"]
}

path "secret-operations/global/mailgun-api-key" {
  capabilities = ["read"]
}

path "secret-operations/sso/ovs" {
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
