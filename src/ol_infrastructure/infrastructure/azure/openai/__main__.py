"""Provision Azure OpenAI capacity and the workload identities that reach it.

Each environment gets one resource group holding one Cognitive Services account per
app consumer, so that usage, cost, and throttling are attributable per app. That
attribution has to come from the resource: Azure Monitor's Cognitive Services metrics
carry no stable caller dimension.

Authentication is workload identity federation. Each consumer gets a user-assigned
managed identity that trusts the environment's EKS cluster OIDC issuer for exactly one
Kubernetes ServiceAccount subject, and holds `Cognitive Services OpenAI User` on
exactly one account. There is no credential to store, rotate, or revoke anywhere in
this project's output.

Managed identities rather than app registrations is the load-bearing choice. An app
registration is an Entra directory object, and creating one needs a Microsoft Graph
app role that only a Privileged Role Administrator can consent to -- the blocker that
killed the Vault Azure secrets engine design (section 9 of
docs/plans/azure-openai-credentials-spec.md). A user-assigned managed identity is an
ARM resource, so everything here is a subscription-scope operation covered by the
Owner grant we already hold. This project performs zero Entra directory writes.
"""

from pathlib import Path

import pulumi_azure_native as azure_native
from pulumi import Config, Output, ResourceOptions, export

from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.pulumi_helper import make_stack_reference, parse_stack

stack_info = parse_stack()
azure_config = Config("azure_openai")

azure_secrets = read_yaml_secrets(
    Path(f"pulumi/azure.{stack_info.env_suffix}.yaml"),
)

# All three consumers run on the applications cluster in every environment, so one
# StackReference covers every federated credential below.
cluster_stack = make_stack_reference(projects.EKS, f"applications.{stack_info.name}")
# Indexed exactly as components/aws/eks.py does when it builds IRSA trust policies
# against the same export.
oidc_issuer = cluster_stack.require_output("cluster_identities").apply(
    lambda identities: identities[0]["oidcs"][0]["issuer"]
)

# Built-in Azure role, identical in every tenant. Data-plane inference only: it grants
# no control over the account, its keys, or its deployments.
COGNITIVE_SERVICES_OPENAI_USER_ROLE_ID = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"

# Azure rejects a federated credential with any other audience count:
# "Federated identity credentials must have exactly one audience".
AZURE_TOKEN_EXCHANGE_AUDIENCE = "api://AzureADTokenExchange"  # noqa: S105

# consumer -> (Kubernetes namespace, ServiceAccount the pods run under). Every field of
# a federated credential is an exact string match with no wildcards, and a wrong value
# creates successfully and fails only later at token exchange, so these are asserted
# here rather than discovered at runtime. Kept in sync with:
#   mitlearn    applications/mit_learn/__main__.py (mitlearn_service_account)
#   learn-ai    applications/learn_ai/__main__.py:254
#   mitxonline  applications/edxapp/k8s_resources.py:252
CONSUMER_SUBJECTS = {
    "mitlearn": ("mitlearn", "mitlearn-app"),
    "learn-ai": ("learn-ai", "learn-ai-admin"),
    "mitxonline": ("mitxonline-openedx", "mitxonline-edxapp-vault"),
}

location = azure_config.get("location") or "eastus"

# Capacity is in thousands of tokens per minute. For the GlobalStandard deployments
# below, quota is pooled per model *and version* across every region in the
# subscription -- not one pool shared by all models, and not per region. So the three
# deployments of a given model in this environment compete with that model's six
# deployments in the other two environments, and with nothing else.
# Ref: https://learn.microsoft.com/en-us/azure/ai-foundry/openai/quotas-limits
#
# Production has no default. The subscription's approved per-model quota is not known
# yet (see the spec's open questions), and a default that silently asks for capacity
# nobody has confirmed turns a bootstrap into a partial deploy: the account and
# identity create, then Deployment creation fails on quota. Non-production gets only
# enough to exercise the code path.
configured_capacity = azure_config.get_int("model_capacity")
if stack_info.env_suffix == "production" and configured_capacity is None:
    msg = (
        "azure_openai:model_capacity must be set explicitly for Production. "
        "Check the subscription's approved TPM quota for each model in "
        f"{location} first -- `az cognitiveservices usage list -l {location}` -- "
        "and remember the value is per model, per deployment across every "
        "environment."
    )
    raise ValueError(msg)
default_capacity = configured_capacity or 5

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

azure_opts = ResourceOptions(provider=azure_provider)

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
workload_identities: dict[str, azure_native.managedidentity.UserAssignedIdentity] = {}

for consumer, (namespace, service_account) in CONSUMER_SUBJECTS.items():
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
            # Keyless. Nothing in this design consumes an Azure account key: pods use
            # WorkloadIdentityCredential, laptops use AzureCliCredential after
            # az login, and the OPENAI_API_KEY fallback this migration preserves
            # targets openai.com rather than Azure. Leaving local auth on would
            # create two live credentials per account that bypass RBAC entirely and
            # that nothing needs.
            disable_local_auth=True,
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

    identity = azure_native.managedidentity.UserAssignedIdentity(
        account_name,
        resource_name_=account_name,
        resource_group_name=resource_group.name,
        location=location,
        tags=resource_tags | {"consumer": consumer},
        opts=azure_opts,
    )
    workload_identities[consumer] = identity

    # One credential per identity. Azure returns 409 on concurrent writes under the
    # same identity, which cannot bite at one apiece; a second would need depends_on.
    azure_native.managedidentity.FederatedIdentityCredential(
        f"{account_name}-federated-credential",
        federated_identity_credential_resource_name="eks-workload-identity",
        resource_name_=identity.name,
        resource_group_name=resource_group.name,
        issuer=oidc_issuer,
        subject=f"system:serviceaccount:{namespace}:{service_account}",
        audiences=[AZURE_TOKEN_EXCHANGE_AUDIENCE],
        opts=azure_opts,
    )

    # Account scope, not resource-group scope. This scoping is the whole reason each
    # consumer gets its own account: it is what stops one app's identity from reaching
    # another app's endpoint.
    azure_native.authorization.RoleAssignment(
        f"{account_name}-openai-user",
        scope=account.id,
        principal_id=identity.principal_id,
        # A freshly created identity that has not yet replicated fails the assignment
        # with PrincipalNotFound unless the type is asserted rather than looked up.
        principal_type="ServicePrincipal",
        role_definition_id=Output.concat(
            "/subscriptions/",
            azure_secrets["subscription_id"],
            "/providers/Microsoft.Authorization/roleDefinitions/",
            COGNITIVE_SERVICES_OPENAI_USER_ROLE_ID,
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
export(
    "workload_identities",
    {
        consumer: {
            "client_id": identity.client_id,
            "principal_id": identity.principal_id,
        }
        for consumer, identity in workload_identities.items()
    },
)
export("tenant_id", azure_secrets["tenant_id"])
export("subscription_id", azure_secrets["subscription_id"])
