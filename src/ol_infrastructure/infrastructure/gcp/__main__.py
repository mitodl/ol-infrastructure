"""Management of Google Cloud Platform projects, credentials and enabled APIs.

One stack. ``mitol01`` is the consolidation target for the whole estate, and
splitting by environment tier would put several stacks on the same GCP project
-- which buys naming rather than isolation, because every role the automation
account needs (``apiKeysAdmin``, ``serviceAccountAdmin``,
``serviceUsageAdmin``) is project-scoped. A ``pulumi-gcp-ci@`` holding
``apiKeysAdmin`` on ``mitol01`` can modify the production key no matter which
stack declares it.

It also costs. Anything project-scoped -- enabled services, org policy, project
metadata -- exists once per project, so each one needs a "declare this in the
Production stack only" carve-out to stop two stacks fighting over a single
resource. One such rule is a footnote; one per resource type is a design.

The tier a credential serves is carried in its own name and restrictions
(``learn-ai-qa`` vs ``learn-ai-production``), which a single stack expresses
without ceremony.

Everything is declared in stack config rather than in this file. A GCP project
is a bag of independently-owned credentials, not a coherent architecture, so
there is no shared Python structure worth expressing -- but there is a great
deal of per-project detail worth keeping under review in YAML.

``ol_gcp:impersonate_service_account`` remains available for the day a stack
needs an identity other than the one the federated credential impersonates.
"""

from typing import Any

from pulumi import Config, Output, ResourceOptions, export

from ol_infrastructure.components.gcp.project import (
    OLGCPAPIKeyConfig,
    OLGCPOIDCProviderConfig,
    OLGCPProject,
    OLGCPProjectConfig,
    OLGCPServiceAccountConfig,
    OLGCPWorkloadIdentityPoolConfig,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.gcp.provider import gcp_provider
from ol_infrastructure.lib.pulumi_helper import make_stack_reference, parse_stack

stack_info = parse_stack()
gcp_config = Config("ol_gcp")

# Optional automation identity override. The federated credential impersonates
# a base account; naming a different one here chains a second impersonation,
# which requires the base account to hold roles/iam.serviceAccountTokenCreator
# on it.
override_service_account = gcp_config.get("impersonate_service_account")

managed_projects = gcp_config.require_object("projects")

gcp_projects: dict[str, OLGCPProject] = {}


def eks_oidc_issuer(eks_stack: str) -> Output[str]:
    """Read an EKS cluster's service-account token issuer from its stack.

    :param eks_stack: Stack name within the EKS project, e.g. ``data.Production``.

    :returns: The cluster's OIDC issuer URL.
    """
    return (
        make_stack_reference(projects.EKS, eks_stack)
        .require_output("cluster_identities")
        .apply(lambda identities: identities[0]["oidcs"][0]["issuer"])
    )


def oidc_provider_config(provider: dict[str, Any]) -> OLGCPOIDCProviderConfig:
    """Build a provider config, resolving ``eks_stack`` to its issuer."""
    provider = dict(provider)
    if eks_stack := provider.pop("eks_stack", None):
        provider["issuer_uri"] = eks_oidc_issuer(eks_stack)
    return OLGCPOIDCProviderConfig(**provider)


for project in managed_projects:
    project_id = project["project_id"]
    region = project.get("region") or "us-east1"

    provider_args = {}
    if override_service_account:
        provider_args["impersonate_service_account"] = override_service_account

    provider = gcp_provider(
        f"gcp-provider-{project_id}",
        project=project_id,
        region=region,
        **provider_args,
    )

    project_config = OLGCPProjectConfig(
        project_id=project_id,
        project_number=project.get("project_number"),
        region=region,
        labels={
            "ou": project["business_unit"],
            "environment": stack_info.env_suffix,
        },
        enabled_services=project.get("enabled_services") or [],
        service_accounts=[
            OLGCPServiceAccountConfig(**account)
            for account in project.get("service_accounts") or []
        ],
        api_keys=[
            OLGCPAPIKeyConfig(**api_key) for api_key in project.get("api_keys") or []
        ],
        workload_identity_pools=[
            OLGCPWorkloadIdentityPoolConfig(
                **{
                    **pool,
                    "oidc_providers": [
                        oidc_provider_config(provider)
                        for provider in pool.get("oidc_providers") or []
                    ],
                }
            )
            for pool in project.get("workload_identity_pools") or []
        ],
    )

    gcp_projects[project_id] = OLGCPProject(
        f"gcp-{project_id}",
        project_config,
        opts=ResourceOptions(provider=provider),
    )

export("gcp_project_ids", sorted(gcp_projects))
export(
    "service_account_emails",
    {
        project_id: gcp_project.service_account_emails
        for project_id, gcp_project in gcp_projects.items()
        if gcp_project.service_account_emails
    },
)
# Consumers build their external_account credential document from this: the
# token audience is "https://iam.googleapis.com/" + name and the credential's
# `audience` field is "//iam.googleapis.com/" + name.
export(
    "workload_identity_providers",
    {
        key: provider.name
        for gcp_project in gcp_projects.values()
        for key, provider in gcp_project.workload_identity_providers.items()
    },
)
