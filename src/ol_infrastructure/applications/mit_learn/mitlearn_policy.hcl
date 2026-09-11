path "aws-mitx/creds/ol-mitlearn-application" {
  capabilities = ["read"]
}
path "aws-mitx/creds/ol-mitlearn-application/*" {
  capabilities = ["read"]
}

path "postgres-mitlearn/creds/app" {
  capabilities = ["read"]
}
path "postgres-mitlearn/creds/app/*" {
  capabilities = ["read"]
}

path "secret-operations/sso/mitlearn/*" {
  capabilities = ["read"]
}
path "secret-operations/sso/mitlearn" {
  capabilities = ["read"]
}
# Sibling of sso/mitlearn, not a child, so the glob above does not cover it.
# Holds the mitlearn-admin-client service account used for the Keycloak Admin API.
path "secret-operations/sso/mitlearn-admin" {
  capabilities = ["read"]
}

path "secret-operations/global/embedly" {
  capabilities = ["read"]
}
path "secret-operations/global/embedly/*" {
  capabilities = ["read"]
}
path "secret-operations/global/odlbot-github-access-token" {
  capabilities = ["read"]
}
path "secret-operations/global/mit-smtp" {
  capabilities = ["read"]
}
path "secret-operations/global/update-search-data-webhook-key" {
  capabilities = ["read"]
}
path "secret-operations/tika/access-token" {
  capabilities = ["read"]
}
path "secret-global/data/mailgun" {
  capabilities = ["read"]
}
path "secret-global/data/shared_hmac" {
  capabilities = ["read"]
}
path "secret-global/data/grafana" {
  capabilities = ["read"]
}

path "secret-mitlearn/*" {
  capabilities = ["read"]
}

# Read-only StarRocks credentials for the warehouse-pull catalog ETL
# (learning_resources.lib.warehouse). The `readonly` role holds SELECT on the
# Iceberg catalog, which is what the integrations__learn__* views live in.
#
# Granted in every environment, including those not yet reading it: a policy
# path pointing at a mount that does not exist, or that nothing reads, grants
# no access on its own. Which stacks actually mint these credentials is decided
# by STARROCKS_HOST in __main__.py, not here.
path "database-starrocks/creds/readonly" {
  capabilities = ["read"]
}
path "database-starrocks/creds/readonly/*" {
  capabilities = ["read"]
}

# XPro HubSpot secret for CRM integration
path "secret-xpro/hubspot" {
  capabilities = ["read"]
}
# vault-secrets-operator is a little more particular about
# managing its own leases, give it the permissions it needs
# for dynamic secret renwals / revocation without giving
# it the power to revoke or renew anything
path "sys/leases/renew" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-mitlearn/creds/app/*", "database-starrocks/creds/readonly/*"]
  }
}
path "sys/leases/revoke" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-mitlearn/creds/app/*", "database-starrocks/creds/readonly/*"]
  }
}
