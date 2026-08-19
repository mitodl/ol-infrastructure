"""Management of Google Cloud Platform projects, credentials and enabled APIs.

Three stacks -- ``CI``, ``QA``, ``Production`` -- each managing every GCP
project whose credentials serve that tier. Not one stack per GCP project: the
GCP project *is* already the environment boundary (``ocw-studio-production``
and ``ocw-studio-qa`` are separate projects), so a stack-per-project layout
would encode that boundary twice. It would also encode it wrongly, because
several legacy project names lie about their tier -- ``ocw-studio-qa`` carries
production YouTube publishing and the estate's largest granted quota. The stack
name states the tier the credentials actually serve; ``project_id`` states
which GCP project happens to hold them today.

Everything is declared in stack config rather than in this file. A GCP project
is a bag of independently-owned credentials, not a coherent architecture, so
there is no shared Python structure worth expressing -- but there is a great
deal of per-project detail worth keeping under review in YAML.

The stack boundary separates what is *declared*. To make it separate what the
deploying identity can *do*, give each stack its own automation service account
via ``ol_gcp:impersonate_service_account`` and grant that account roles only on
the projects in its tier. Without that, every stack shares one identity and the
CI stack retains write access to production projects.
"""

from pulumi import Config, ResourceOptions, export

from ol_infrastructure.components.gcp.project import (
    OLGCPAPIKeyConfig,
    OLGCPProject,
    OLGCPProjectConfig,
    OLGCPServiceAccountConfig,
)
from ol_infrastructure.lib.gcp.provider import gcp_provider
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()
gcp_config = Config("ol_gcp")

# Optional per-stack automation identity. The federated credential impersonates
# a base account; naming a different one here chains a second impersonation,
# which requires the base account to hold roles/iam.serviceAccountTokenCreator
# on it. This is what turns the stack boundary into a permission boundary.
tier_service_account = gcp_config.get("impersonate_service_account")

managed_projects = gcp_config.require_object("projects")

gcp_projects: dict[str, OLGCPProject] = {}

for project in managed_projects:
    project_id = project["project_id"]
    region = project.get("region") or "us-east1"

    provider_args = {}
    if tier_service_account:
        provider_args["impersonate_service_account"] = tier_service_account

    provider = gcp_provider(
        f"gcp-provider-{project_id}",
        project=project_id,
        region=region,
        **provider_args,
    )

    project_config = OLGCPProjectConfig(
        project_id=project_id,
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
