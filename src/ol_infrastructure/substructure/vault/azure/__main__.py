"""Mount Vault's Azure secrets engine for Azure OpenAI access.

Consumes the accounts and root service principal created by
``infrastructure/azure/openai`` and exposes one role per app consumer. Reading
``azure-openai/creds/<role>`` mints a short-lived Azure AD service principal holding
``Cognitive Services OpenAI User`` on **that consumer's account only**, which is what
stops one application's credentials from reaching another's endpoint.

Credential issuance fails until a tenant Global Admin has granted admin consent on the
root application -- see the README in ``infrastructure/azure/openai``. Every resource
here applies cleanly without it.
"""

from pulumi import export

from ol_infrastructure.components.services.vault import (
    OLVaultAzureRoleConfig,
    OLVaultAzureSecretsEngine,
    OLVaultAzureSecretsEngineConfig,
)
from ol_infrastructure.lib.pulumi_helper import make_stack_reference, parse_stack
from ol_infrastructure.lib.pulumi_projects import AZURE_OPENAI
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
stack_info = parse_stack()

azure_openai_stack = make_stack_reference(AZURE_OPENAI, stack_info.name)

cognitive_accounts = azure_openai_stack.require_output("cognitive_accounts")

# Built-in Azure role granting data-plane inference access and nothing else -- no
# ability to read keys, change deployments, or alter the account.
COGNITIVE_SERVICES_OPENAI_USER = "Cognitive Services OpenAI User"

# Vault role name -> the consumer key used in the infrastructure stack's outputs.
AZURE_OPENAI_ROLES = {
    "ol-mitlearn-openai": "mitlearn",
    "ol-learn-ai-openai": "learn-ai",
    "ol-mitxonline-openai": "mitxonline",
}

azure_openai_secrets_engine = OLVaultAzureSecretsEngine(
    OLVaultAzureSecretsEngineConfig(
        app_name="openai",
        vault_backend_path="azure-openai",
        description=(
            "Dynamic Azure AD service principals scoped to Azure OpenAI inference "
            f"for the {stack_info.env_suffix} environment"
        ),
        subscription_id=azure_openai_stack.require_output(
            "vault_root_sp_subscription_id"
        ),
        tenant_id=azure_openai_stack.require_output("vault_root_sp_tenant_id"),
        client_id=azure_openai_stack.require_output("vault_root_sp_client_id"),
        client_secret=azure_openai_stack.require_output("vault_root_sp_client_secret"),
        roles={
            role_name: OLVaultAzureRoleConfig(
                role_name=COGNITIVE_SERVICES_OPENAI_USER,
                scope=cognitive_accounts[consumer]["id"],
            )
            for role_name, consumer in AZURE_OPENAI_ROLES.items()
        },
    )
)

export("azure_openai_mount_path", "azure-openai")
export("azure_openai_roles", list(AZURE_OPENAI_ROLES))
export(
    "cognitive_account_endpoints",
    {
        consumer: cognitive_accounts[consumer]["endpoint"]
        for consumer in AZURE_OPENAI_ROLES.values()
    },
)
export("model_deployments", azure_openai_stack.require_output("model_deployments"))
