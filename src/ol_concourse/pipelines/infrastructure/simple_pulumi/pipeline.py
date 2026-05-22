# ruff: noqa: PLR0915, PLR0912, C901
"""Generate Concourse pipeline definitions for simple Pulumi-only deployments.

This template is for applications/services that only need Pulumi infrastructure
deployment across CI, QA, and Production stages without any build steps.
"""

import sys
from pathlib import Path
from typing import Any

from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import Duration, GetStep, Identifier, Pipeline
from ol_concourse.lib.resources import git_repo, github_issues, registry_image
from pydantic import BaseModel, model_validator

from ol_concourse.pipelines.constants import (
    GH_ISSUES_DEFAULT_REPOSITORY,
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)
from ol_concourse.pipelines.jobs import pulumi_jobs_chain


class DockerImageConfig(BaseModel):
    """Configuration for a Docker image input.

    Attributes:
        image_repository: Docker image repository (e.g., "kodhive/leek").
        image_tag: Optional tag to watch (default: "latest").
        username: Optional Docker registry username.
        password: Optional Docker registry password.
        env_var_for_digest: Optional environment variable name to pass digest to Pulumi.
        env_var_for_tag: Optional environment variable name to pass tag to Pulumi.
    """

    image_repository: str
    image_tag: str | None = "latest"
    username: str | None = None
    password: str | None = None
    env_var_for_digest: str | None = None
    env_var_for_tag: str | None = None


class SimplePulumiParams(BaseModel):
    """Parameters for simple Pulumi-only pipeline.

    Attributes:
        app_name: The name of the application/service.
        pulumi_project_path: Path to Pulumi project relative to src/ol_infrastructure/.
        pulumi_project_name: Name of the Pulumi project (e.g. "ol-application-tika").
        stack_prefix: Short deployment-scope discriminator that appears before the
                      stage in the stack name (e.g. "mitlearn" for qdrant-cloud →
                      stack name "mitlearn.QA", "operations" for vector-log-proxy →
                      "operations.QA"). Leave empty for single-tenant projects whose
                      stack names are just the stage (e.g. "QA", "Production").
                      Not used when deployment_groups is set.
        stages: List of deployment stages (default: ["CI", "QA", "Production"]).
        deployment_groups: List of deployment group names for multi-group deployments
                          (e.g. ["mitx", "mitxonline", "xpro"] for mongodb_atlas).
                          If specified, auto_discover_stacks will be used.
        auto_discover_stacks: If True, automatically discover stacks from repo
                             (default: False unless deployment_groups is set).
        additional_watched_paths: Additional paths to watch beyond PULUMI_WATCHED_PATHS.
        branch: Git branch to watch (default: "main").
        docker_image: Optional Docker image configuration for apps that depend on
                     external Docker images.
        prior_stage_stack: Short stack name of the preceding stage that runs in a
                          different Concourse environment (e.g. "lakehouse.QA" when
                          this pipeline only runs the Production stage in prod
                          Concourse). When set, a GitHub issues trigger resource is
                          added so this pipeline gates on the prior stage's deployment
                          issue being closed, preserving the same promotion workflow
                          used within a single chained pipeline.
    """

    app_name: str
    pulumi_project_path: str
    pulumi_project_name: str
    stack_prefix: str = ""
    stages: list[str] = ["CI", "QA", "Production"]
    deployment_groups: list[str] | None = None
    auto_discover_stacks: bool = False
    additional_watched_paths: list[str] = []
    branch: str = "main"
    docker_image: DockerImageConfig | None = None
    prior_stage_stack: str | None = None

    @model_validator(mode="before")
    @classmethod
    def set_defaults(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Set default values for optional fields."""
        # Auto-enable stack discovery if deployment_groups is specified
        if data.get("deployment_groups") and not data.get("auto_discover_stacks"):
            data["auto_discover_stacks"] = True
        return data


def discover_pulumi_stacks(
    project_path: Path, deployment_groups: list[str] | None = None
) -> dict[str, list[str]] | list[str]:
    """Discover Pulumi stacks from the filesystem.

    Args:
        project_path: Path to the Pulumi project directory.
        deployment_groups: Optional list of deployment groups to discover.

    Returns:
        If deployment_groups is specified: Dict mapping group names to list of
        stack names for that group (sorted by stage: CI → QA → Production).
        If deployment_groups is None: List of stack names sorted by stage.
    """
    stack_files = list(project_path.glob("Pulumi.*.yaml"))

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
            if stack_name == stage or stack_name.endswith(f".{stage}"):
                return priority
        return 999  # Unknown stages go last

    # If deployment groups specified, return grouped dict
    if deployment_groups:
        grouped_stacks: dict[str, list[str]] = {}
        for group in deployment_groups:
            group_stacks = [stack for stack in stacks if stack.startswith(f"{group}.")]
            # Sort by stage priority within group
            group_stacks.sort(key=get_stage_priority)
            if group_stacks:
                grouped_stacks[group] = group_stacks
        return grouped_stacks
    else:
        # No deployment groups - return simple list sorted by stage
        stacks.sort(key=get_stage_priority)
        return stacks


pipeline_params: dict[str, SimplePulumiParams] = {
    "airbyte": SimplePulumiParams(
        app_name="airbyte",
        pulumi_project_path="applications/airbyte/",
        pulumi_project_name="ol-application-airbyte",
        additional_watched_paths=[
            "src/bridge/secrets/airbyte/",
            "src/bridge/lib/versions.py",
        ],
    ),
    "celery-monitoring": SimplePulumiParams(
        app_name="celery-monitoring",
        pulumi_project_path="applications/celery_monitoring/",
        pulumi_project_name="ol-application-celery-monitoring",
        docker_image=DockerImageConfig(
            image_repository="kodhive/leek",
            image_tag="0.7.5",  # Must match LEEK_VERSION in bridge.lib.versions
        ),
    ),
    "clickhouse": SimplePulumiParams(
        app_name="clickhouse",
        pulumi_project_path="applications/clickhouse/",
        pulumi_project_name="ol-application-clickhouse",
        stages=["CI", "QA", "Production"],
    ),
    "data_warehouse": SimplePulumiParams(
        app_name="data_warehouse",
        pulumi_project_path="infrastructure/aws/data_warehouse/",
        pulumi_project_name="ol-infrastructure-data-warehouse",
        stages=["QA", "Production"],
    ),
    "digital-credentials": SimplePulumiParams(
        app_name="digital-credentials",
        pulumi_project_path="applications/digital_credentials/",
        pulumi_project_name="ol-application-digital-credentials",
        additional_watched_paths=["src/bridge/secrets/digital_credentials/"],
    ),
    "fastly-redirector": SimplePulumiParams(
        app_name="fastly-redirector",
        pulumi_project_path="applications/fastly_redirector/",
        pulumi_project_name="ol-application-fastly-redirector",
    ),
    "jupyterhub-data": SimplePulumiParams(
        app_name="jupyterhub-data",
        pulumi_project_path="applications/jupyterhub_data/",
        pulumi_project_name="ol-application-jupyterhub-data",
        additional_watched_paths=["src/bridge/secrets/jupyterhub_data/"],
    ),
    "marimo-data": SimplePulumiParams(
        app_name="marimo-data",
        pulumi_project_path="applications/marimo_data/",
        pulumi_project_name="ol-application-marimo-data",
    ),
    "mongodb-atlas": SimplePulumiParams(
        app_name="mongodb-atlas",
        pulumi_project_path="infrastructure/mongodb_atlas/",
        pulumi_project_name="ol-infrastructure-mongodb-atlas",
        deployment_groups=["mitx", "mitx-staging", "mitxonline", "xpro"],
    ),
    "ocw-site": SimplePulumiParams(
        app_name="ocw-site",
        pulumi_project_path="applications/ocw_site/",
        pulumi_project_name="ol-application-ocw-site",
        stages=["QA", "Production"],
    ),
    "open-discussions": SimplePulumiParams(
        app_name="open-discussions",
        pulumi_project_path="applications/open_discussions/",
        pulumi_project_name="ol-application-open-discussions",
        stages=["QA", "Production"],
    ),
    "open-metadata": SimplePulumiParams(
        app_name="open-metadata",
        pulumi_project_path="applications/open_metadata/",
        pulumi_project_name="ol-application-open-metadata",
        additional_watched_paths=[
            "src/bridge/secrets/open_metadata/",
            "src/bridge/lib/versions.py",
        ],
    ),
    "open-metadata-substructure": SimplePulumiParams(
        app_name="open-metadata-substructure",
        pulumi_project_path="substructure/open_metadata/",
        pulumi_project_name="ol-substructure-open-metadata",
        additional_watched_paths=[
            "src/bridge/secrets/open_metadata/",
            "src/bridge/lib/versions.py",
        ],
        stages=["QA", "Production"],
    ),
    "opensearch": SimplePulumiParams(
        app_name="opensearch",
        pulumi_project_path="infrastructure/aws/opensearch/",
        pulumi_project_name="ol-infrastructure-opensearch",
        deployment_groups=[
            "apps",
            "celery_monitoring",
            "mitlearn",
            "mitx",
            "mitx-staging",
            "mitxonline",
            "open",
            "open_metadata",
            "xpro",
        ],
    ),
    "qdrant-cloud": SimplePulumiParams(
        app_name="qdrant-cloud",
        pulumi_project_path="infrastructure/qdrant_cloud/",
        pulumi_project_name="ol-infrastructure-qdrant-cloud",
        stack_prefix="mitlearn",
        additional_watched_paths=[
            "src/bridge/secrets/qdrant_cloud/",
            "src/bridge/lib/versions.py",
        ],
    ),
    "starrocks": SimplePulumiParams(
        app_name="starrocks",
        pulumi_project_path="applications/starrocks/",
        pulumi_project_name="ol-application-starrocks",
        deployment_groups=[
            "lakehouse",
        ],
        stages=["QA", "Production"],
    ),
    # The substructure stack makes direct TCP connections to the StarRocks NLB, which
    # is internal to the data VPC.  Each ops VPC is only peered with its same-env data
    # VPC, so the QA stage must run in QA Concourse and Production in prod Concourse.
    # See starrocks-substructure-qa below for the QA-Concourse entry.
    "starrocks-substructure": SimplePulumiParams(
        app_name="starrocks_substructure",
        pulumi_project_path="substructure/starrocks/",
        pulumi_project_name="ol-substructure-starrocks",
        deployment_groups=[
            "lakehouse",
        ],
        stages=["Production"],
        prior_stage_stack="lakehouse.QA",
    ),
    "starrocks-substructure-qa": SimplePulumiParams(
        app_name="starrocks_substructure",
        pulumi_project_path="substructure/starrocks/",
        pulumi_project_name="ol-substructure-starrocks",
        deployment_groups=[
            "lakehouse",
        ],
        stages=["QA"],
    ),
    "tika": SimplePulumiParams(
        app_name="tika",
        pulumi_project_path="applications/tika/",
        pulumi_project_name="ol-application-tika",
    ),
    "vector-log-proxy": SimplePulumiParams(
        app_name="vector-log-proxy",
        pulumi_project_path="infrastructure/vector_log_proxy/",
        pulumi_project_name="ol-infrastructure-vector-log-proxy",
        stack_prefix="operations",
    ),
    "aws-ecr": SimplePulumiParams(
        app_name="aws-ecr",
        pulumi_project_path="infrastructure/aws/ecr/",
        pulumi_project_name="ol-infrastructure-ecr",
        stages=["default"],
    ),
    "aws-sftp": SimplePulumiParams(
        app_name="aws-sftp",
        pulumi_project_path="infrastructure/aws/sftp_servers/",
        pulumi_project_name="ol-infrastructure-aws-sftp",
    ),
    "b2b-partners-storage": SimplePulumiParams(
        app_name="b2b-partners-storage",
        pulumi_project_path="applications/b2b_partners_storage/",
        pulumi_project_name="ol-application-b2b-partners-storage",
    ),
    "mailgun": SimplePulumiParams(
        app_name="mailgun",
        pulumi_project_path="applications/mailgun/",
        pulumi_project_name="ol-application-mailgun",
    ),
    "monitoring": SimplePulumiParams(
        app_name="monitoring",
        pulumi_project_path="infrastructure/monitoring/",
        pulumi_project_name="ol-infrastructure-monitoring",
        stages=["default"],
    ),
    "starburst": SimplePulumiParams(
        app_name="starburst",
        pulumi_project_path="applications/starburst/",
        pulumi_project_name="ol-application-starburst",
        stages=["Production"],
    ),
    "xpro-partner-dns": SimplePulumiParams(
        app_name="xpro-partner-dns",
        pulumi_project_path="substructure/xpro_partner_dns/",
        pulumi_project_name="ol-substructure-xpro-partner-dns",
        stages=["default"],
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
            "src/bridge/lib/versions.py",
            *params.additional_watched_paths,
        ],
    )

    # Set up Docker image resource if configured
    docker_image_resource = None
    docker_dependencies = []
    docker_env_vars_from_files = {}

    if params.docker_image:
        docker_image_resource = registry_image(
            name=Identifier(f"{app_name}-docker-image"),
            image_repository=params.docker_image.image_repository,
            image_tag=params.docker_image.image_tag,
            username=params.docker_image.username,
            password=params.docker_image.password,
        )
        docker_dependencies.append(
            GetStep(get=docker_image_resource.name, trigger=True)
        )

        # Set up environment variables from docker image files if configured
        if params.docker_image.env_var_for_digest:
            docker_env_vars_from_files[params.docker_image.env_var_for_digest] = (
                f"{docker_image_resource.name}/digest"
            )
        if params.docker_image.env_var_for_tag:
            docker_env_vars_from_files[params.docker_image.env_var_for_tag] = (
                f"{docker_image_resource.name}/tag"
            )

    # Build cross-environment GitHub issue gate if this pipeline continues a chain
    # that started in a different Concourse environment.  The prior stage's deployment
    # posts a GitHub issue on success; this pipeline watches for it to be closed before
    # allowing the first job here to run, preserving the normal promotion gate.
    cross_env_custom_deps: dict[int, list[GetStep]] = {}
    cross_env_resources = []
    if params.prior_stage_stack:
        prior_trigger = github_issues(
            auth_method="token",
            name=Identifier(
                f"github-issues-{params.pulumi_project_name}-{params.prior_stage_stack.lower()}-trigger"
            ),
            repository=GH_ISSUES_DEFAULT_REPOSITORY,
            issue_title_template=(
                f"[bot] Pulumi {params.pulumi_project_name}"
                f" {params.prior_stage_stack} deployed."
            ),
            issue_prefix=(
                f"[bot] Pulumi {params.pulumi_project_name}"
                f" {params.prior_stage_stack} deployed."
            ),
            issue_state="closed",
            poll_frequency=Duration("15m"),
        )
        cross_env_resources.append(prior_trigger)
        cross_env_custom_deps = {0: [GetStep(get=prior_trigger.name, trigger=True)]}

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
            project_full_path, params.deployment_groups
        )
        if not discovered_stacks:
            msg = f"No stacks discovered for {app_name} at {project_full_path}"
            raise ValueError(msg)

        # Filter discovered stacks to only the configured stages.  This ensures
        # that stages=["Production"] works correctly even when auto_discover_stacks
        # is enabled (which would otherwise include all stage YAML files on disk).
        # An empty stages list means "all stages" (no filtering).
        if params.stages:
            allowed = set(params.stages)
            if isinstance(discovered_stacks, dict):
                discovered_stacks = {
                    group: [
                        s
                        for s in stacks
                        if any(
                            s == stage or s.endswith(f".{stage}") for stage in allowed
                        )
                    ]
                    for group, stacks in discovered_stacks.items()
                }
                # Drop any groups that became empty after filtering
                discovered_stacks = {g: s for g, s in discovered_stacks.items() if s}
            else:
                discovered_stacks = [
                    s
                    for s in discovered_stacks
                    if any(s == stage or s.endswith(f".{stage}") for stage in allowed)
                ]
        if not discovered_stacks:
            msg = (
                f"No stacks discovered for {app_name} at {project_full_path} "
                f"after filtering for stages {params.stages}"
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
                    refresh_stack=True,
                    project_name=params.pulumi_project_name,
                    stack_names=group_stacks,
                    project_source_path=PULUMI_CODE_PATH.joinpath(
                        params.pulumi_project_path
                    ),
                    dependencies=docker_dependencies,
                    env_vars_from_files=docker_env_vars_from_files or None,
                    custom_dependencies=cross_env_custom_deps or None,
                )

                # Collect resources and jobs
                all_resource_types.extend(group_fragment.resource_types)
                all_resources.extend(group_fragment.resources)
                all_jobs.extend(group_fragment.jobs)

            # Combine all fragments
            all_pipeline_resources = [pulumi_code, *cross_env_resources, *all_resources]
            if docker_image_resource:
                all_pipeline_resources.append(docker_image_resource)

            combined_fragment = PipelineFragment(
                resource_types=all_resource_types,
                resources=all_pipeline_resources,
                jobs=all_jobs,
            )
        else:
            # Single chain for simple discovered stacks
            pulumi_fragment = pulumi_jobs_chain(
                pulumi_code,
                refresh_stack=True,
                project_name=params.pulumi_project_name,
                stack_names=discovered_stacks,
                project_source_path=PULUMI_CODE_PATH.joinpath(
                    params.pulumi_project_path
                ),
                dependencies=docker_dependencies,
                env_vars_from_files=docker_env_vars_from_files or None,
                custom_dependencies=cross_env_custom_deps or None,
            )

            all_pipeline_resources = [
                pulumi_code,
                *cross_env_resources,
                *pulumi_fragment.resources,
            ]
            if docker_image_resource:
                all_pipeline_resources.append(docker_image_resource)

            combined_fragment = PipelineFragment(
                resource_types=pulumi_fragment.resource_types,
                resources=all_pipeline_resources,
                jobs=pulumi_fragment.jobs,
            )
    else:
        # Use explicitly configured stages - single chain.
        # When stack_prefix is set (e.g. "mitlearn", "operations"), the stack
        # name is "{prefix}.{stage}"; otherwise it is just the stage.
        stack_names = [
            f"{params.stack_prefix}.{stage}" if params.stack_prefix else stage
            for stage in params.stages
        ]

        pulumi_fragment = pulumi_jobs_chain(
            pulumi_code,
            refresh_stack=True,
            project_name=params.pulumi_project_name,
            stack_names=stack_names,
            project_source_path=PULUMI_CODE_PATH.joinpath(params.pulumi_project_path),
            dependencies=docker_dependencies,
            env_vars_from_files=docker_env_vars_from_files or None,
            custom_dependencies=cross_env_custom_deps or None,
        )

        all_pipeline_resources = [
            pulumi_code,
            *cross_env_resources,
            *pulumi_fragment.resources,
        ]
        if docker_image_resource:
            all_pipeline_resources.append(docker_image_resource)

        combined_fragment = PipelineFragment(
            resource_types=pulumi_fragment.resource_types,
            resources=all_pipeline_resources,
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
