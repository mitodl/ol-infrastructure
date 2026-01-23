
path "aws-mitx/creds/ocw-studio-app-{stack_info.env_suffix}" {
  capabilities = ["read"]
}

path "secret-global/mailgun" {
  capabilities = ["read"]
}

path "secret-concourse/ocw/api-bearer-token" {
  capabilities = ["read"]
}

path "secret-concourse/web" {
  capabilities = ["read"]
}
