# Migrating an environment to docker pre-steps

1. In vault, create `secret-<app>/<app>-wildcard-certificate/cert` which is _just_ the certificate from the `cert_chain`. It is the first cert in the chain entry and can just be copied and pasted into the new key.
2. Reconcile the waffle flags from the django admin page for the environment.
  a. Verify that default flags match.
  b. Add any required additional flags to the pulumi configuration file for the environment. Be sure to include `--create`.
