#  noqa: WPS232
from collections.abc import Iterable
from copy import deepcopy
from pathlib import Path
from typing import Optional

from concourse.lib.constants import REGISTRY_IMAGE
from concourse.lib.models.fragment import PipelineFragment
from concourse.lib.models.pipeline import (
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
from concourse.lib.resource_types import (
    packer_build,
    packer_validate,
    pulumi_provisioner_resource,
)
from concourse.lib.resources import pulumi_provisioner


def packer_jobs(  # noqa: PLR0913
    dependencies: list[GetStep],
    image_code: Resource,
    packer_template_path: str = "src/bilder/images/.",
    node_types: Optional[Iterable[str]] = None,
    packer_vars: Optional[dict[str, str]] = None,
    env_vars_from_files: Optional[dict[str, str]] = None,
    extra_packer_params: Optional[dict[str, str]] = None,
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


def pulumi_jobs_chain(  # noqa: PLR0913
    pulumi_code: Resource,
    stack_names: list[str],
    project_name: str,
    project_source_path: Path,
    custom_dependencies: Optional[dict[int, list[GetStep]]] = None,
    dependencies: Optional[list[GetStep]] = None,
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
    :type custom_dependencies: Dict[int, list[GetStep]]

    :returns: A `PipelineFragment` object that can be composed with other fragments to
              build a full pipeline.
    """
    chain_fragment = PipelineFragment()
    previous_job = None
    for index, stack_name in enumerate(stack_names):
        production_stack = stack_name.lower().endswith("production")
        passed_param = None
        if index != 0:
            previous_job = chain_fragment.jobs[-1]
            passed_param = [previous_job.name]
        for dependency in dependencies or []:
            # These mutations apply globally if the dependencies aren't copied below
            dependency.trigger = not bool(previous_job or production_stack)
            dependency.passed = passed_param or dependency.passed  # type: ignore

        # Need to copy the dependencies because otherwise they are globally mutated
        local_dependencies = [
            dependency_step.copy() for dependency_step in (dependencies or [])
        ]
        if custom_dependency := (custom_dependencies or {}).get(index):
            local_custom_dependencies = [
                custom_dependency_step.copy()
                for custom_dependency_step in custom_dependency
            ]
            local_dependencies.extend(local_custom_dependencies)

        step_fragment = pulumi_job(
            pulumi_code,
            stack_name,
            project_name,
            project_source_path,
            local_dependencies,
            previous_job,
        )
        chain_fragment.resource_types = (
            chain_fragment.resource_types + step_fragment.resource_types
        )
        chain_fragment.resources = chain_fragment.resources + step_fragment.resources
        chain_fragment.jobs.extend(step_fragment.jobs)
    return chain_fragment


def pulumi_job(  # noqa: PLR0913
    pulumi_code: Resource,
    stack_name: str,
    project_name: str,
    project_source_path: Path,
    dependencies: Optional[list[GetStep]] = None,
    previous_job: Optional[Job] = None,
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
        name=Identifier("pulumi-project"),
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
                        path=f"{pulumi_code.name}/pipelines/infrastructure/scripts/generate_aws_config_from_instance_profile.sh"  # noqa: E501
                    ),
                ),
            ),
            PutStep(
                put=pulumi_resource.name,
                get_params={"skip_implicit_get": True},
                params={
                    "env_os": {
                        "AWS_DEFAULT_REGION": "us-east-1",
                        "PYTHONPATH": f"/usr/lib/:/tmp/build/put/{pulumi_code.name}/src/",  # noqa: E501
                    },
                    "stack_name": stack_name,
                },
            ),
        ],
    )
    return PipelineFragment(
        resources=[pulumi_resource],
        resource_types=[pulumi_provisioner_resource_type],
        jobs=[pulumi_job_object],
    )
