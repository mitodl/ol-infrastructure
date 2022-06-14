## Secret Layout

```
secret-airbyte/pomerium :
{
  "allowed_group": "",
  "application_address": "",
  "authenticate_server_address": "",
  "cookie_secret": "",
  "dagster_hashed_password": "",
  "dagster_password": "",
  "idp_client_id": "",
  "idp_client_secret": "",
  "idp_service_account": "",
  "signing_key": ""
}

secret-airbyte/sentry-dsn :
{
  "value": ""
}
```
## Descriptions

- `sentry_dsn:value`: This is the DSN for the sentry application obtained from the configuration in sentry.
- `allowed_group`: This is the team slug. You may not be able to get this until you expirment with pomerium a bit and see what it lists as available team slugs.
- `application_address`: This is the DNS entry for the application, I.E. `airbyte-qa.odl.mit.edu`.
- `authenticate_server_address`: This is the DNS entry for the auth service, I.E. `airbyte-qa-auth.odl.mit.edu`.
- `cookie_secret`: 256 bit of random data. Generate with `head -c32 /dev/urandom | base64`
- `dagster_hashed_password`: The basic auth password for the dagster bypass. Should be hashed as so `openssl passwd -apr1`
- `dagster_password`: The basic auth password for the dagster bypass. This key is never referenced in code but provided for engineering referral.
- `idp_client_id`: The OAuth applicaiton ID from github. Found near the top as 'Cient ID'.
- `idp_client_secret`: The OAuth application client secret. You can generate multiple ones but Github will only give you the value when you first generate it. If you fail to record it, you will need to generate a new one.
- `idp_service_account`: A base64 encoded
- `idp_client_id`: The OAuth applicaiton ID from github. Found near the top as 'Cient ID'.
- `idp_client_secret`: The OAuth application client secret. You can generate multiple ones but Github will only give you the value when you first generate it. If you fail to record it, you will need to generate a new one.
- `idp_service_account`: A base64 encoded JSON object representing the username and api token for interacting with the github account. There should be no new-lines in the base64 encoding. This should probably be linked to a shared github account or bot account. More information [here](https://www.pomerium.com/docs/identity-providers/github.html#create-a-service-account).
- `signing_key`: This is used for the JWT token authentication between the app and pomerium. More information [here](https://github.com/pomerium/pomerium/blob/main/scripts/generate_self_signed_signing_key.sh) and [here](https://www.pomerium.com/docs/reference/signing-key).
```
# Generates an P-256 (ES256) signing key
openssl ecparam  -genkey  -name prime256v1  -noout  -out ec_private.pem
# careful! this will output your private key in terminal
cat ec_private.pem | base64
# You can safely discard the ec_private.pem file once you've recorded the base64 encoded value in vault
```

## GitHub OAuth Application Setup

Follow the instructions [here](https://www.pomerium.com/docs/identity-providers/github.html#create-a-service-account).
