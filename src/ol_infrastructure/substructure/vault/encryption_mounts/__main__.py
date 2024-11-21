import pulumi_vault as vault

from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

stack_info = parse_stack()
setup_vault_provider(stack_info)

vault_infrastructure_transit = vault.Mount(
    "vault-infrastructure-transit-encryption-provider",
    type="transit",
    path="infrastructure",
)

vault_sops_infrastructure_key = vault.transit.SecretBackendKey(
    "vault-sops-infrastructure-key",
    name="sops",
    backend=vault_infrastructure_transit.path,
    type="aes256-gcm96",
    deletion_allowed=True,
)

vault_secrets_operator_key = vault.transit.SecretBackendKey(
    "vault-secrets-operator-key",
    name="vault-secrets-operator",
    backend=vault_infrastructure_transit.path,
    type="aes256-gcm96",
    deletion_allowed=True,
)
