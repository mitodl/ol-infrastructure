path "secret-operations/global/cloudfront-private-key" {
  capabilities = ["read"]
}
path "secret_operations/global/mailgun-api-key" {
  capabilities = ["read"]
}

path "postgres-rc-odlvideo/creds/odlvideo" {
  capabilities = ["read"]
}

path "secret-odl-video-service/" {
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
