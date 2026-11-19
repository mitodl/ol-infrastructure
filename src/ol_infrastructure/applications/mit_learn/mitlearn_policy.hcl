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
# ol-mitlearn-memory-client, which exchanges a learner's session token for one
# the memory service (Hindsight) accepts. Granted in every environment; only
# those where the keycloak substructure enables ol_memory.py have it to read.
path "secret-operations/sso/mitlearn-memory" {
  capabilities = ["read"]
}
# mit-learn's Production Keycloak client for this environment, which ol-llm
# authenticates to Opik with.
path "secret-opik/sdk-mit-learn" {
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
# (learning_resources.lib.warehouse). The `batch_readonly` role holds readonly's
# SELECT on the Iceberg catalog, which is what the integrations__learn__* views
# live in, and puts the pull's queries in the batch resource group.
#
# Granted in every environment, including those not yet reading it: a policy
# path pointing at a mount that does not exist, or that nothing reads, grants
# no access on its own. Which stacks actually mint these credentials is decided
# by STARROCKS_HOST in __main__.py, not here.
path "database-starrocks/creds/batch_readonly" {
  capabilities = ["read"]
}
path "database-starrocks/creds/batch_readonly/*" {
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
    lease_id = ["postgres-mitlearn/creds/app/*", "database-starrocks/creds/batch_readonly/*"]
  }
}
path "sys/leases/revoke" {
  capabilities = ["update"]
  allowed_parameters = {
    lease_id = ["postgres-mitlearn/creds/app/*", "database-starrocks/creds/batch_readonly/*"]
  }
}
