"""Concourse pipeline jobs for infrastructure provisioning and management."""

from collections.abc import Iterable
from copy import deepcopy
from pathlib import Path

from bridge.settings.github.team_members import DEVOPS_MIT
from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    InParallelStep,
    Input,
    Job,
    Output,
    Platform,
    PutStep,
    RegistryImage,
    Resource,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resource_types import (
    github_issues_resource,
    packer_build,
    packer_validate,
    pulumi_provisioner_resource,
)
from ol_concourse.lib.resources import github_issues, pulumi_provisioner
from ol_concourse.pipelines.constants import GH_ISSUES_DEFAULT_REPOSITORY


def packer_jobs(  # noqa: PLR0913
    dependencies: list[GetStep],
    image_code: Resource,
    packer_template_path: str = "src/bilder/images/.",
    node_types: Iterable[str] | None = None,
    packer_vars: dict[str, str] | None = None,
    env_vars_from_files: dict[str, str] | None = None,
    extra_packer_params: dict[str, str] | None = None,
    job_name_suffix: str = "",
) -> PipelineFragment:
    """Generate a pipeline fragment for building EC2 AMIs with Packer.

    :param dependencies: The list of `Get` steps that should be run at the start of the
        pipeline.  This is used for setting up inputs to the build, as well as for
        triggering on upstream changes (e.g. GitHub releases).
    :param image_code: The Git resource definition that specifies the repository that
        holds the code for building the image, including the Packer template.
    :param packer_template_path: The path in the image_code resource that points to the
        Packer template that you would like to build.
    :param node_types: The node types that should be built for the template and passed
        as vars during the build (e.g. web and worker)
    :param packer_vars: A dictionary of var inputs for the Packer template.
    :param env_vars_from_files: The list of environment variables that should be set
        during the build and the files to load for populating the values (e.g. the
        `version` file from a GitHub resource)
    :param extra_packer_params: A dictionary of parameters to pass to the `packer`
        command line (e.g. `-only` or `-except` when you want to specify a particular
        build target)
    :param job_name_suffix: A string to append to the name of the validate and build
        jobs to allow for ensuring unique names when multiple Packer builds happen in a
        single pipeline.

    :returns: A `PipelineFragment` object that can be composed with other fragments to
              build a complete pipeline definition.
    """
    packer_validate_type = packer_validate()
    packer_build_type = packer_build()
    packer_build_resource = Resource(name="packer-build", type=packer_build_type.name)
    packer_validate_resource = Resource(
        name="packer-validate", type=packer_validate_type.name
    )
    validate_job = Job(
        name=Identifier(f"validate-packer-template-{job_name_suffix}".strip("-")),
        plan=[
            *dependencies,
            GetStep(get=image_code.name, trigger=True),
            InParallelStep(
                in_parallel=[
                    PutStep(
                        put=packer_validate_resource.name,
                        params={
                            "template": f"{image_code.name}/{packer_template_path}",
                            "objective": "validate",
                            "vars": {
                                **(packer_vars or {}),
                                **{"node_type": node_type},  # noqa: PIE800
                            },
                            **(extra_packer_params or {}),
                        },
                    )
                    for node_type in node_types or ["server"]
                ]
            ),
        ],
    )
    # Make sure that all of the dependencies have passed the validate step before
    # triggering the image build.
    build_deps = [deepcopy(dep) for dep in dependencies]
    for dep in build_deps:
        dep.passed = [validate_job.name]
    build_job = Job(
        name=Identifier(f"build-packer-template-{job_name_suffix}".strip("-")),
        plan=[
            *build_deps,
            GetStep(get=image_code.name, trigger=True, passed=[validate_job.name]),
            InParallelStep(
                in_parallel=[
                    PutStep(
                        attempts=3,
                        put=packer_build_resource.name,
                        params={
                            "template": f"{image_code.name}/{packer_template_path}",
                            "objective": "build",
                            "vars": {
                                **(packer_vars or {}),
                                **{"node_type": node_type},  # noqa: PIE800
                            },
                            "env_vars": {
                                "AWS_REGION": "us-east-1",
                                "PYTHONPATH": f"${{PYTHONPATH}}:{image_code.name}/src",
                                "PACKER_GITHUB_API_TOKEN": "((github.public_repo_access_token))",  # noqa: E501
                            },
                            "env_vars_from_files": env_vars_from_files or {},
                            **(extra_packer_params or {}),
                        },
                    )
                    for node_type in node_types or ["server"]
                ]
            ),
        ],
    )
    return PipelineFragment(
        resource_types=[packer_validate_type, packer_build_type],
        resources=[packer_validate_resource, packer_build_resource],
        jobs=[validate_job, build_job],
    )


def pulumi_jobs_chain(  # noqa: PLR0913, C901, PLR0912
    pulumi_code: Resource,
    stack_names: list[str],
    project_name: str,
    project_source_path: Path,
    enable_github_issue_resource: bool = True,  # noqa: FBT001, FBT002
    custom_dependencies: dict[int, list[GetStep]] | None = None,
    dependencies: list[GetStep] | None = None,
    additional_post_steps: dict[int, list[GetStep | PutStep | TaskStep]] | None = None,
    github_issue_assignees: list[str] | None = None,
    github_issue_labels: list[str] | None = None,
    github_issue_repository: str | None = None,
    additional_env_vars: dict[str, str] | None = None,
) -> PipelineFragment:
    """Create a chained sequence of jobs for running Pulumi tasks.

    :param pulumi_code: A git resource that represents the repository for the code being
        executed
    :param stack_names: The list of stack names in sequence that should be chained
        together
    :param project_name: The name of the Pulumi project being executed
    :param project_source_path: The path within the `pulumi_code` resource where the
        code being executed is located
    :param dependencies: A list of `Get` step definitions that are used as inputs or
        triggers for the jobs in the chain
    :param custom_dependencies: A dict of indices and `Get` step definitions that are
        used as inputs or triggers for the jobs in the chain.
    :param github_issue_assignees: A list of GitHub usernames that should be assigned
    :param github_issue_labels: A list of GitHub labels that should be applied
    :type custom_dependencies: Dict[int, list[GetStep]]

    :returns: A `PipelineFragment` object that can be composed with other fragments to
              build a full pipeline.
    """

    chain_fragment = PipelineFragment(resource_types=[github_issues_resource()])
    previous_job = None
    gh_issues_trigger = None
    for index, stack_name in enumerate(stack_names):
        if index + 1 < len(stack_names) and enable_github_issue_resource:
            gh_issues_trigger = github_issues(
                auth_method="token",
                name=Identifier(f"github-issues-{stack_name.lower()}-trigger"),
                repository=github_issue_repository or GH_ISSUES_DEFAULT_REPOSITORY,
                issue_title_template=f"[bot] Pulumi {project_name} {stack_name} "
                "deployed.",
                issue_prefix=f"[bot] Pulumi {project_name} {stack_name} deployed.",
                issue_state="closed",
                poll_frequency="15m",
            )

        if enable_github_issue_resource:
            gh_issues_post = github_issues(
                auth_method="token",
                name=Identifier(f"github-issues-{stack_name.lower()}-post"),
                repository=github_issue_repository or GH_ISSUES_DEFAULT_REPOSITORY,
                issue_title_template=(
                    f"[bot] Pulumi {project_name} {stack_name} deployed."
                ),
                issue_prefix=(f"[bot] Pulumi {project_name} {stack_name} deployed."),
                issue_state="open",
            )

        production_stack = stack_name.lower().endswith("production")
        qa_stack = stack_name.lower().endswith("qa")
        ci_stack = stack_name.lower().endswith("ci")

        passed_param = None
        if index != 0:
            previous_stack = stack_names[index - 1]
            previous_job = chain_fragment.jobs[-1]
            passed_param = [previous_job.name]

        for dependency in dependencies or []:
            # These mutations apply globally if the dependencies aren't copied below
            if hasattr(dependency, "trigger"):
                dependency.trigger = not bool(previous_job or production_stack)
                dependency.passed = passed_param or dependency.passed

        # Need to copy the dependencies because otherwise they are globally mutated
        local_dependencies = [
            dependency_step.model_copy() for dependency_step in (dependencies or [])
        ]
        # Needed to duplicate if conditional because otherwise it messes with the
        # sequencing of dependencies and whether they had to pass previous stacks.
        if index != 0 and enable_github_issue_resource:
            # We don't want the current stage, we want the previous one so that it will
            # trigger the current stack. This ensures that we are triggering on the
            # notification that the previous step has been deployed.
            get_gh_issues = GetStep(
                get=Identifier(f"github-issues-{previous_stack.lower()}-trigger"),
                trigger=True,
            )
            local_dependencies.append(get_gh_issues)

        if custom_dependency := (custom_dependencies or {}).get(index):
            local_custom_dependencies = [
                custom_dependency_step.model_copy()
                for custom_dependency_step in custom_dependency
            ]
            local_dependencies.extend(local_custom_dependencies)

        step_fragment = pulumi_job(
            pulumi_code,
            stack_name,
            project_name,
            project_source_path,
            local_dependencies,
            (additional_post_steps or {}).get(index, []),
            previous_job,
            additional_env_vars=additional_env_vars,
        )

        default_github_issue_labels = [
            "product:infrastructure",
            "DevOps",
            "pipeline-workflow",
        ]
        if ci_stack:
            default_github_issue_labels.append("promotion-to-qa")
        elif qa_stack:
            default_github_issue_labels.append("promotion-to-production")
        elif production_stack:
            default_github_issue_labels.append("finalized-deployment")

        if enable_github_issue_resource:
            create_gh_issue = PutStep(
                put=gh_issues_post.name,
                params={
                    "labels": github_issue_labels or default_github_issue_labels,
                    "assignees": github_issue_assignees or DEVOPS_MIT,
                },
            )
            chain_fragment.resources.append(gh_issues_post)
            step_fragment.jobs[0].on_success = create_gh_issue

        chain_fragment.resource_types = (
            chain_fragment.resource_types + step_fragment.resource_types
        )
        chain_fragment.resources = chain_fragment.resources + step_fragment.resources

        if gh_issues_trigger:
            chain_fragment.resources.append(gh_issues_trigger)
        chain_fragment.jobs.extend(step_fragment.jobs)

    return chain_fragment


def pulumi_job(  # noqa: PLR0913
    pulumi_code: Resource,
    stack_name: str,
    project_name: str,
    project_source_path: Path,
    dependencies: list[GetStep] | None = None,
    additional_post_steps: list[GetStep | PutStep | TaskStep] | None = None,
    previous_job: Job | None = None,
    additional_env_vars: dict[str, str] | None = None,
) -> PipelineFragment:
    """Create a job definition for running a Pulumi task.

    :param pulumi_code: A git resource that represents the repository for the code being
        executed
    :param stack_name: The stack name to use while executing the Pulumi task
    :param project_name: The name of the Pulumi project being executed
    :param project_source_path: The path within the `pulumi_code` resource where the
        code being executed is located
    :param dependencies: A list of `Get` step definitions that are used as inputs or
        triggers for the jobs in the chain
    :param previous_job: The job object that should be added as a `passed` dependency
        for the `get` step input for this job definition.

    :returns: A `PipelineFragment` object that can be composed with other fragments to
              build a full pipeline.
    """
    pulumi_provisioner_resource_type = pulumi_provisioner_resource()
    pulumi_resource = pulumi_provisioner(
        name=Identifier(f"pulumi-{project_name}"),
        project_name=project_name,
        project_path=f"{pulumi_code.name}/{project_source_path}",
    )
    passed_job = [previous_job.name] if previous_job else None
    aws_creds_path = Output(name=Identifier("aws_creds"))
    pulumi_job_object = Job(
        name=Identifier(f"deploy-{project_name}-{stack_name.lower()}"),
        max_in_flight=1,  # Only allow 1 Pulumi task at a time since they lock anyway.
        plan=(dependencies or [])
        + [
            GetStep(
                get=pulumi_code.name,
                trigger=passed_job is None
                and not stack_name.lower().endswith("production"),
                passed=passed_job,
            ),
            TaskStep(
                task=Identifier("set-aws-creds"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type=REGISTRY_IMAGE,
                        source=RegistryImage(repository="amazon/aws-cli"),
                    ),
                    inputs=[Input(name=pulumi_code.name)],
                    outputs=[aws_creds_path],
                    run=Command(
                        path=(
                            f"{pulumi_code.name}/pipelines/infrastructure/scripts/"
                            "generate_aws_config_from_instance_profile.sh"
                        )
                    ),
                ),
            ),
            PutStep(
                put=pulumi_resource.name,
                get_params={"skip_implicit_get": True},
                params={
                    "env_os": {
                        "AWS_DEFAULT_REGION": "us-east-1",
                        "PYTHONPATH": (
                            f"/usr/lib/:/tmp/build/put/{pulumi_code.name}/src/"
                        ),
                        "GITHUB_TOKEN": "((github.public_repo_access_token))",
                        **(additional_env_vars or {}),
                    },
                    "stack_name": stack_name,
                },
            ),
        ]
        + (additional_post_steps or []),
    )
    return PipelineFragment(
        resources=[pulumi_resource],
        resource_types=[pulumi_provisioner_resource_type],
        jobs=[pulumi_job_object],
    )
