# Vault policy for the vuln-scanner Kubernetes deployment.
#
# Intentionally empty: v1 has no authenticated scanning and no Vault-managed
# secrets (S3 + Security Hub access both go through IRSA, not Vault dynamic
# credentials). OLEKSAuthBindingConfig requires either vault_policy_path or
# vault_policy_text to be set regardless, since it always provisions the
# Vault Kubernetes auth binding alongside IRSA -- this file exists to satisfy
# that, not because any secret is granted through it yet.
#
# Add real path stanzas here if/when authenticated ZAP scanning is added
# (a Keycloak test-user credential pulled from Vault) -- see the plan's
# "Deferred" section.
