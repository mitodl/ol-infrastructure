"""Provision Azure OpenAI capacity and the identity Vault uses to broker access to it.

Each environment gets one resource group holding one Cognitive Services account per
app consumer, so that usage, cost, and throttling are attributable per app. That
attribution has to come from the resource: Azure Monitor's Cognitive Services metrics
carry no stable caller dimension, and Vault mints a fresh service principal on every
lease, so grouping by calling identity is unworkable by construction.

The service principal created here is Vault's *root* credential -- the identity Vault
authenticates as in order to mint the short-lived per-app principals. It is not used by
any application directly. Granting it User Access Administrator on the resource group
(rather than the subscription) is what bounds the blast radius of a Vault compromise to
these OpenAI accounts.
"""

from pathlib import Path

import pulumi_azure_native as azure_native
import pulumi_azuread as azuread
from pulumi import Config, Output, ResourceOptions, export

from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()
azure_config = Config("azure_openai")

azure_secrets = read_yaml_secrets(
    Path(f"pulumi/azure.{stack_info.env_suffix}.yaml"),
)

# Well-known Azure identifiers. These are global constants, identical in every tenant.
MICROSOFT_GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
# Graph *application* permission letting Vault manage only the applications it owns --
# deliberately narrower than Application.ReadWrite.All. Requires tenant admin consent,
# which Pulumi cannot grant; see the README for the one-time manual step.
GRAPH_APPLICATION_READWRITE_OWNEDBY_ROLE_ID = "18a4783c-866b-4cc7-a460-3d5e5662c884"
USER_ACCESS_ADMINISTRATOR_ROLE_ID = "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9"

# The consumers that get their own account. Keys become the Vault role suffixes in
# substructure/vault/azure, so they must stay in sync with the role names there.
APP_CONSUMERS = ("mitlearn", "learn-ai", "mitxonline")

location = azure_config.get("location") or "eastus"

# Capacity is in thousands of tokens per minute and is drawn from a single regional
# pool shared by every deployment in the subscription. With three models on each of
# nine accounts there are 27 deployments dividing that pool, so non-production gets
# only enough to exercise the code path.
default_capacity = azure_config.get_int("model_capacity") or (
    50 if stack_info.env_suffix == "production" else 5
)

# gpt-4o and gpt-4o-mini are learn-ai's current defaults; gpt-5.2 is what edxapp's
# translations config already asks OpenAI for. Deploying all three is what lets Azure
# stand in for both consumers without changing either app's model settings.
model_names = azure_config.get_object("models") or [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-5.2",
]

# Version strings are deliberately not hardcoded here -- which versions exist is a
# property of the subscription and region, not of this code, and a wrong string fails
# at Deployment create time rather than at preview. Left unset, Azure deploys its
# current default version for the model and upgrades it automatically over time.
#
# Pin them once confirmed against the target subscription with
# `az cognitiveservices account list-models -n <account> -g <rg>`: set
# `azure_openai:model_versions` to a {model: version} map in the stack config, which
# also switches that model to NoAutoUpgrade so a new default version cannot change
# model behaviour under a running application with no deploy and no diff.
model_versions: dict[str, str] = azure_config.get_object("model_versions") or {}

azure_provider = azure_native.Provider(
    "azure-openai-provider",
    subscription_id=azure_secrets["subscription_id"],
    tenant_id=azure_secrets["tenant_id"],
    client_id=azure_secrets["client_id"],
    client_secret=azure_secrets["client_secret"],
    location=location,
)
azuread_provider = azuread.Provider(
    "azure-openai-azuread-provider",
    tenant_id=azure_secrets["tenant_id"],
    client_id=azure_secrets["client_id"],
    client_secret=azure_secrets["client_secret"],
)

azure_opts = ResourceOptions(provider=azure_provider)
azuread_opts = ResourceOptions(provider=azuread_provider)

resource_tags = {
    "OU": "operations",
    "Environment": stack_info.name,
    "pulumi_managed": "true",
    "pulumi_stack": stack_info.full_name,
}

resource_group = azure_native.resources.ResourceGroup(
    f"ol-openai-{stack_info.env_suffix}",
    resource_group_name=f"ol-openai-{stack_info.env_suffix}",
    location=location,
    tags=resource_tags,
    opts=azure_opts,
)

cognitive_accounts: dict[str, azure_native.cognitiveservices.Account] = {}
deployment_names: dict[str, list[str]] = {}

for consumer in APP_CONSUMERS:
    account_name = f"ol-openai-{consumer}-{stack_info.env_suffix}"
    account = azure_native.cognitiveservices.Account(
        account_name,
        account_name=account_name,
        resource_group_name=resource_group.name,
        location=location,
        kind="OpenAI",
        sku=azure_native.cognitiveservices.SkuArgs(name="S0"),
        properties=azure_native.cognitiveservices.AccountPropertiesArgs(
            # Required for Entra ID token auth. Without a custom subdomain the
            # account only accepts API-key auth, which is exactly what this project
            # exists to stop using.
            custom_sub_domain_name=account_name,
            public_network_access="Enabled",
            # Left enabled deliberately: this migration is additive and the existing
            # static-key wiring stays working alongside it. Flipping this to true is
            # the final step of the migration, once no app depends on key auth.
            disable_local_auth=False,
        ),
        tags=resource_tags | {"consumer": consumer},
        opts=azure_opts,
    )
    cognitive_accounts[consumer] = account

    deployment_names[consumer] = []
    for model_name in model_names:
        pinned_version = model_versions.get(model_name)
        azure_native.cognitiveservices.Deployment(
            f"{account_name}-{model_name}",
            deployment_name=model_name,
            account_name=account.name,
            resource_group_name=resource_group.name,
            sku=azure_native.cognitiveservices.SkuArgs(
                name="GlobalStandard",
                capacity=default_capacity,
            ),
            properties=azure_native.cognitiveservices.DeploymentPropertiesArgs(
                model=azure_native.cognitiveservices.DeploymentModelArgs(
                    format="OpenAI",
                    name=model_name,
                    version=pinned_version,
                ),
                version_upgrade_option=(
                    "NoAutoUpgrade"
                    if pinned_version
                    else "OnceNewDefaultVersionAvailable"
                ),
            ),
            opts=azure_opts,
        )
        deployment_names[consumer].append(model_name)

# Vault's own identity. One per environment, so a compromised Vault in CI cannot mint
# credentials against Production's accounts.
vault_root_application = azuread.Application(
    f"vault-azure-openai-{stack_info.env_suffix}",
    display_name=f"vault-azure-openai-{stack_info.env_suffix}",
    description=(
        "Root credential for Vault's Azure secrets engine. Used to mint short-lived "
        "service principals for Azure OpenAI access. Managed by Pulumi in "
        f"{stack_info.full_name}."
    ),
    sign_in_audience="AzureADMyOrg",
    required_resource_accesses=[
        azuread.ApplicationRequiredResourceAccessArgs(
            resource_app_id=MICROSOFT_GRAPH_APP_ID,
            resource_accesses=[
                azuread.ApplicationRequiredResourceAccessResourceAccessArgs(
                    id=GRAPH_APPLICATION_READWRITE_OWNEDBY_ROLE_ID,
                    type="Role",
                )
            ],
        )
    ],
    opts=azuread_opts,
)

vault_root_service_principal = azuread.ServicePrincipal(
    f"vault-azure-openai-sp-{stack_info.env_suffix}",
    client_id=vault_root_application.client_id,
    description=(
        "Service principal Vault authenticates as when minting Azure OpenAI "
        "credentials."
    ),
    opts=azuread_opts,
)

vault_root_password = azuread.ApplicationPassword(
    f"vault-azure-openai-password-{stack_info.env_suffix}",
    application_id=vault_root_application.id,
    display_name="vault-azure-secrets-engine",
    opts=azuread_opts,
)

# Scoped to the resource group, not the subscription. Vault needs to create role
# assignments for the principals it mints; this bounds where it can create them.
azure_native.authorization.RoleAssignment(
    f"vault-azure-openai-user-access-admin-{stack_info.env_suffix}",
    scope=resource_group.id,
    principal_id=vault_root_service_principal.object_id,
    principal_type="ServicePrincipal",
    role_definition_id=Output.concat(
        "/subscriptions/",
        azure_secrets["subscription_id"],
        "/providers/Microsoft.Authorization/roleDefinitions/",
        USER_ACCESS_ADMINISTRATOR_ROLE_ID,
    ),
    opts=azure_opts,
)

export("resource_group_id", resource_group.id)
export("resource_group_name", resource_group.name)
export("location", location)
export(
    "cognitive_accounts",
    {
        consumer: {
            "id": account.id,
            "endpoint": account.properties.endpoint,
        }
        for consumer, account in cognitive_accounts.items()
    },
)
export("model_deployments", deployment_names)
export("vault_root_sp_client_id", vault_root_application.client_id)
export("vault_root_sp_object_id", vault_root_service_principal.object_id)
export("vault_root_sp_tenant_id", azure_secrets["tenant_id"])
export("vault_root_sp_subscription_id", azure_secrets["subscription_id"])
export("vault_root_sp_client_secret", Output.secret(vault_root_password.value))
