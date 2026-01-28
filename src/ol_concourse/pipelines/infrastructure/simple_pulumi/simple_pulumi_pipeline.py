"""Generate Concourse pipeline definitions for simple Pulumi-only deployments.

This template is for applications/services that only need Pulumi infrastructure
deployment across CI, QA, and Production stages without any build steps.
"""

import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, model_validator

from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS


class SimplePulumiParams(BaseModel):
    """Parameters for simple Pulumi-only pipeline.

    Attributes:
        app_name: The name of the application/service.
        pulumi_project_path: Path to Pulumi project relative to src/ol_infrastructure/.
        stack_prefix: Prefix for Pulumi stack names (e.g., "applications.tika").
        pulumi_project_name: Name of the Pulumi project.
        stages: List of deployment stages (default: ["CI", "QA", "Production"]).
        deployment_groups: List of deployment group names for multi-group deployments
                          (e.g., ["mitx", "mitxonline", "xpro"] for mongodb_atlas).
                          If specified, auto_discover_stacks will be used.
        auto_discover_stacks: If True, automatically discover stacks from repo
                             (default: False unless deployment_groups is set).
        additional_watched_paths: Additional paths to watch beyond PULUMI_WATCHED_PATHS.
        branch: Git branch to watch (default: "main").
    """

    app_name: str
    pulumi_project_path: str
    stack_prefix: str
    pulumi_project_name: str | None = None
    stages: list[str] = ["CI", "QA", "Production"]
    deployment_groups: list[str] | None = None
    auto_discover_stacks: bool = False
    additional_watched_paths: list[str] = []
    branch: str = "main"

    @model_validator(mode="before")
    @classmethod
    def set_defaults(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Set default values for optional fields."""
        if not data.get("pulumi_project_name"):
            data["pulumi_project_name"] = f"ol-infrastructure-{data['app_name']}"
        # Auto-enable stack discovery if deployment_groups is specified
        if data.get("deployment_groups") and not data.get("auto_discover_stacks"):
            data["auto_discover_stacks"] = True
        return data


def discover_pulumi_stacks(
    project_path: Path, stack_prefix: str, deployment_groups: list[str] | None = None
) -> dict[str, list[str]] | list[str]:
    """Discover Pulumi stacks from the filesystem.

    Args:
        project_path: Path to the Pulumi project directory.
        stack_prefix: Stack prefix to match (e.g., "infrastructure.mongodb_atlas").
        deployment_groups: Optional list of deployment groups to discover.

    Returns:
        If deployment_groups is specified: Dict mapping group names to list of
        stack names for that group (sorted by stage: CI → QA → Production).
        If deployment_groups is None: List of stack names sorted by stage.
    """
    stack_files = list(project_path.glob(f"Pulumi.{stack_prefix}.*.yaml"))

    stacks = []
    for stack_file in stack_files:
        # Extract stack name from filename: Pulumi.{stack_name}.yaml
        stack_name = stack_file.stem.replace("Pulumi.", "")
        stacks.append(stack_name)

    # Stage priority for sorting
    stage_priority = {"CI": 0, "QA": 1, "Production": 2}

    def get_stage_priority(stack_name: str) -> int:
        """Extract stage from stack name and return priority."""
        for stage, priority in stage_priority.items():
            if stack_name.endswith(f".{stage}"):
                return priority
        return 999  # Unknown stages go last

    # If deployment groups specified, return grouped dict
    if deployment_groups:
        grouped_stacks: dict[str, list[str]] = {}
        for group in deployment_groups:
            group_stacks = [
                stack
                for stack in stacks
                if stack.startswith(f"{stack_prefix}.{group}.")
            ]
            # Sort by stage priority within group
            group_stacks.sort(key=get_stage_priority)
            if group_stacks:
                grouped_stacks[group] = group_stacks
        return grouped_stacks
    else:
        # No deployment groups - return simple list sorted by stage
        stacks.sort(key=get_stage_priority)
        return stacks


# Pipeline parameter configurations for each app
pipeline_params: dict[str, SimplePulumiParams] = {
    "airbyte": SimplePulumiParams(
        app_name="airbyte",
        pulumi_project_path="applications/airbyte/",
        stack_prefix="applications.airbyte",
        additional_watched_paths=[
            "src/bridge/secrets/airbyte/",
            "src/bridge/lib/versions.py",
        ],
    ),
    "digital-credentials": SimplePulumiParams(
        app_name="digital-credentials",
        pulumi_project_path="applications/digital_credentials/",
        stack_prefix="applications.digital_credentials",
        additional_watched_paths=["src/bridge/secrets/digital_credentials/"],
    ),
    "fastly-redirector": SimplePulumiParams(
        app_name="fastly-redirector",
        pulumi_project_path="applications/fastly_redirector/",
        stack_prefix="applications.fastly_redirector",
        pulumi_project_name="ol-infrastructure-fastly-redirector",
    ),
    "kubewatch": SimplePulumiParams(
        app_name="kubewatch",
        pulumi_project_path="applications/kubewatch/",
        stack_prefix="applications.kubewatch",
    ),
    "micromasters": SimplePulumiParams(
        app_name="micromasters",
        pulumi_project_path="applications/micromasters/",
        stack_prefix="applications.micromasters",
        pulumi_project_name="ol-infrastructure-micromasters-application",
        additional_watched_paths=["src/bridge/lib/"],
    ),
    "mongodb-atlas": SimplePulumiParams(
        app_name="mongodb-atlas",
        pulumi_project_path="infrastructure/mongodb_atlas/",
        stack_prefix="infrastructure.mongodb_atlas",
        deployment_groups=["mitx", "mitx-staging", "mitxonline", "xpro"],
    ),
    "ocw-studio": SimplePulumiParams(
        app_name="ocw-studio",
        pulumi_project_path="applications/ocw_studio/",
        stack_prefix="applications.ocw_studio",
        pulumi_project_name="ol-infrastructure-ocw_studio-application",
        additional_watched_paths=["src/bridge/secrets/ocw_studio/"],
    ),
    "open-discussions": SimplePulumiParams(
        app_name="open-discussions",
        pulumi_project_path="applications/open_discussions/",
        stack_prefix="applications.open_discussions",
        pulumi_project_name="ol-infrastructure-open_discussions-application",
        stages=["QA", "Production"],
    ),
    "open-metadata": SimplePulumiParams(
        app_name="open-metadata",
        pulumi_project_path="applications/open_metadata/",
        stack_prefix="applications.open_metadata",
        additional_watched_paths=[
            "src/bridge/secrets/open_metadata/",
            "src/bridge/lib/versions.py",
        ],
    ),
    "opensearch": SimplePulumiParams(
        app_name="opensearch",
        pulumi_project_path="infrastructure/aws/opensearch/",
        stack_prefix="infrastructure.aws.opensearch",
        pulumi_project_name="ol-infrastructure-opensearch",
        deployment_groups=[
            "apps",
            "mitx",
            "mitx-staging",
            "mitxonline",
            "xpro",
            "mitopen",
            "mitlearn",
            "celery_monitoring",
            "open_metadata",
        ],
    ),
    "tika": SimplePulumiParams(
        app_name="tika",
        pulumi_project_path="applications/tika/",
        stack_prefix="applications.tika",
        pulumi_project_name="ol-infrastructure-tika-server",
    ),
    "vector-log-proxy": SimplePulumiParams(
        app_name="vector-log-proxy",
        pulumi_project_path="infrastructure/vector_log_proxy/",
        stack_prefix="infrastructure.vector_log_proxy.operations",
    ),
    "xpro": SimplePulumiParams(
        app_name="xpro",
        pulumi_project_path="applications/xpro/",
        stack_prefix="applications.xpro",
        pulumi_project_name="ol-infrastructure-xpro-application",
        additional_watched_paths=["src/bridge/secrets/xpro"],
    ),
    "xpro-partner-dns": SimplePulumiParams(
        app_name="xpro-partner-dns",
        pulumi_project_path="substructure/xpro_partner_dns/",
        stack_prefix="substructure.xpro_partner_dns",
        pulumi_project_name="ol-infrastructure-substructure-xpro-partner-dns",
        stages=[""],
    ),
    "bootcamps": SimplePulumiParams(
        app_name="bootcamps",
        pulumi_project_path="applications/bootcamps/",
        stack_prefix="applications.bootcamps",
        pulumi_project_name="ol-infrastructure-bootcamps-application",
        additional_watched_paths=["src/bridge/lib/"],
    ),
}


def build_simple_pulumi_pipeline(app_name: str) -> Pipeline:
    """Generate a simple Pulumi-only pipeline for a given application.

    Args:
        app_name: The name of the application to generate a pipeline for.

    Returns:
        A complete Concourse Pipeline object.

    Raises:
        ValueError: If app_name is not found in pipeline_params.
    """
    if app_name not in pipeline_params:
        msg = (
            f"Application '{app_name}' not found in pipeline_params. "
            f"Available apps: {', '.join(pipeline_params.keys())}"
        )
        raise ValueError(msg)

    params = pipeline_params[app_name]

    # Define git resource for Pulumi code
    pulumi_code = git_repo(
        name=Identifier(f"ol-infrastructure-pulumi-{app_name}"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch=params.branch,
        paths=[
            *PULUMI_WATCHED_PATHS,
            str(PULUMI_CODE_PATH.joinpath(params.pulumi_project_path)),
            *params.additional_watched_paths,
        ],
    )

    # Determine stack names to use
    if params.auto_discover_stacks:
        # Auto-discover stacks from the repository
        # Find repository root by looking for .git directory
        repo_root = Path(__file__).resolve()
        while repo_root.parent != repo_root:
            if (repo_root / ".git").exists():
                break
            repo_root = repo_root.parent

        project_full_path = (
            repo_root / "src/ol_infrastructure" / params.pulumi_project_path
        )
        discovered_stacks = discover_pulumi_stacks(
            project_full_path, params.stack_prefix, params.deployment_groups
        )
        if not discovered_stacks:
            msg = (
                f"No stacks discovered for {app_name} at {project_full_path} "
                f"with prefix {params.stack_prefix}"
            )
            raise ValueError(msg)

        # If deployment groups are used, create separate chains for parallel execution
        if isinstance(discovered_stacks, dict):
            all_resource_types = []
            all_resources = []
            all_jobs = []

            # Share a single git resource across all deployment groups
            for group_stacks in discovered_stacks.values():
                # Create a job chain for this deployment group
                group_fragment = pulumi_jobs_chain(
                    pulumi_code,
                    project_name=params.pulumi_project_name,
                    stack_names=group_stacks,
                    project_source_path=PULUMI_CODE_PATH.joinpath(
                        params.pulumi_project_path
                    ),
                    dependencies=[],
                )

                # Collect resources and jobs
                all_resource_types.extend(group_fragment.resource_types)
                all_resources.extend(group_fragment.resources)
                all_jobs.extend(group_fragment.jobs)

            # Combine all fragments
            combined_fragment = PipelineFragment(
                resource_types=all_resource_types,
                resources=[
                    pulumi_code,
                    *all_resources,
                ],
                jobs=all_jobs,
            )
        else:
            # Single chain for simple discovered stacks
            pulumi_fragment = pulumi_jobs_chain(
                pulumi_code,
                project_name=params.pulumi_project_name,
                stack_names=discovered_stacks,
                project_source_path=PULUMI_CODE_PATH.joinpath(
                    params.pulumi_project_path
                ),
                dependencies=[],
            )

            combined_fragment = PipelineFragment(
                resource_types=pulumi_fragment.resource_types,
                resources=[
                    pulumi_code,
                    *pulumi_fragment.resources,
                ],
                jobs=pulumi_fragment.jobs,
            )
    else:
        # Use explicitly configured stages - single chain
        stack_names = [
            params.stack_prefix if stage == "" else f"{params.stack_prefix}.{stage}"
            for stage in params.stages
        ]

        pulumi_fragment = pulumi_jobs_chain(
            pulumi_code,
            project_name=params.pulumi_project_name,
            stack_names=stack_names,
            project_source_path=PULUMI_CODE_PATH.joinpath(params.pulumi_project_path),
            dependencies=[],
        )

        combined_fragment = PipelineFragment(
            resource_types=pulumi_fragment.resource_types,
            resources=[
                pulumi_code,
                *pulumi_fragment.resources,
            ],
            jobs=pulumi_fragment.jobs,
        )

    return combined_fragment.to_pipeline()


if __name__ == "__main__":
    min_args = 2
    if len(sys.argv) < min_args:
        msg = (
            "Please provide an app name as a command line argument.\n"
            f"Available apps: {', '.join(pipeline_params.keys())}"
        )
        raise ValueError(msg)

    app_name = sys.argv[1]

    try:
        pipeline = build_simple_pulumi_pipeline(app_name)
        with open("definition.json", "w") as definition:  # noqa: PTH123
            definition.write(pipeline.model_dump_json(indent=2))
        sys.stdout.write(pipeline.model_dump_json(indent=2))
        print()  # noqa: T201
        print(f"fly -t pr-inf sp -p pulumi-{app_name} -c definition.json")  # noqa: T201
    except ValueError as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)
