"""Management of Google Cloud Platform projects, credentials and enabled APIs.

One stack per GCP project, named ``<tenant>.<Environment>`` so that the stack
name says which application owns the project and which environment it serves --
``ocw-studio.Production``, ``mit-learn.QA``. The legacy estate does not follow
that shape (``ocw-studio-qa`` holds production YouTube publishing, and
``recaptcha-migrated-075600d5919`` is a machine-generated name), which is
precisely why the stack name carries the intended meaning and
``gcp_project:project_id`` carries the literal GCP project id it currently
maps to. The two converge as projects are consolidated; until then the mapping
is explicit and visible in one file per stack.

Everything is declared in stack config rather than in this file. A GCP project
is a bag of independently-owned credentials, not a coherent architecture, so
there is no shared Python structure worth expressing -- but there is a great
deal of per-project detail worth keeping under review in YAML.
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
gcp_config = Config("gcp_project")

project_id = gcp_config.require("project_id")
business_unit = gcp_config.require("business_unit")
region = gcp_config.get("region") or "us-east1"

provider = gcp_provider(
    f"gcp-provider-{project_id}",
    project=project_id,
    region=region,
)

project_config = OLGCPProjectConfig(
    project_id=project_id,
    region=region,
    labels={
        "ou": business_unit,
        "environment": stack_info.env_suffix,
    },
    enabled_services=gcp_config.get_object("enabled_services") or [],
    service_accounts=[
        OLGCPServiceAccountConfig(**account)
        for account in gcp_config.get_object("service_accounts") or []
    ],
    api_keys=[
        OLGCPAPIKeyConfig(**api_key)
        for api_key in gcp_config.get_object("api_keys") or []
    ],
)

gcp_project = OLGCPProject(
    f"gcp-{stack_info.env_prefix or project_id}-{stack_info.env_suffix}",
    project_config,
    opts=ResourceOptions(provider=provider),
)

export("gcp_project_id", gcp_project.project_id)
export("service_account_emails", gcp_project.service_account_emails)
