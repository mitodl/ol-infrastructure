from collections.abc import Iterable
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


def packer_jobs(
    dependencies: list[GetStep],
    image_code: Resource,
    packer_template_path: str = "src/bilder/images/.",
    node_types: Optional[Iterable[str]] = None,
    packer_vars: Optional[dict[str, str]] = None,
    env_vars_from_files: Optional[dict[str, str]] = None,
) -> PipelineFragment:
    packer_validate_type = packer_validate()
    packer_build_type = packer_build()
    packer_build_resource = Resource(name="packer-build", type=packer_build_type.name)
    packer_validate_resource = Resource(
        name="packer-validate", type=packer_validate_type.name
    )
    validate_job = Job(
        name=Identifier("validate-packer-template"),
        plan=dependencies
        + [
            GetStep(
                get=image_code.name,
                trigger=True,
            ),
            InParallelStep(
                in_parallel=[
                    PutStep(
                        put=packer_validate_resource.name,
                        params={
                            "template": f"{image_code.name}/src/bilder/images/.",
                            "objective": "validate",
                            "vars": {**(packer_vars or {}), **{"node_type": node_type}},
                        },
                    )
                    for node_type in (node_types or ["server"])
                ]
            ),
        ],
    )
    build_job = Job(
        name=Identifier("build-packer-template"),
        plan=dependencies
        + [
            GetStep(
                get=image_code.name,
                trigger=True,
                passed=[validate_job.name],
            ),
            InParallelStep(
                in_parallel=[
                    PutStep(
                        put=packer_build_resource.name,
                        params={
                            "template": f"{image_code.name}/{packer_template_path}",
                            "objective": "build",
                            "vars": {**(packer_vars or {}), **{"node_type": node_type}},
                            "env_vars": {
                                "AWS_REGION": "us-east-1",
                                "PYTHONPATH": f"${{PYTHONPATH}}:{image_code.name}/src",
                            },
                            "env_vars_from_files": env_vars_from_files or {},
                            "only": ["amazon-ebs.third-party"],
                        },
                    )
                    for node_type in (node_types or ["server"])
                ]
            ),
        ],
    )
    return PipelineFragment(
        resource_types=[packer_validate_type, packer_build_type],
        resources=[packer_validate_resource, packer_build_resource],
        jobs=[validate_job, build_job],
    )


def pulumi_jobs_chain(
    pulumi_code: Resource,
    stack_names: list[str],
    project_name: str,
    project_source_path: Path,
    dependencies: Optional[list[GetStep]] = None,
) -> PipelineFragment:
    chain_fragment = PipelineFragment()
    previous_job = None
    for index, stack_name in enumerate(stack_names):
        if index != 0:
            previous_job = chain_fragment.jobs[-1]
            for dependency in dependencies or []:
                dependency.trigger = False
        step_fragment = pulumi_job(
            pulumi_code,
            stack_name,
            project_name,
            project_source_path,
            dependencies,
            previous_job,
        )
        chain_fragment.resource_types.extend(step_fragment.resource_types)
        chain_fragment.resources.extend(step_fragment.resources)
        chain_fragment.jobs.extend(step_fragment.jobs)
    return chain_fragment


def pulumi_job(
    pulumi_code: Resource,
    stack_name: str,
    project_name: str,
    project_source_path: Path,
    dependencies: Optional[list[GetStep]] = None,
    previous_job: Optional[Job] = None,
) -> PipelineFragment:
    pulumi_provisioner_resource_type = pulumi_provisioner_resource()
    packer_build_type = packer_build()
    packer_build_resource = Resource(name="packer-build", type=packer_build_type.name)
    pulumi_resource = pulumi_provisioner(
        name=Identifier("pulumi-project"),
        project_name=project_name,
        project_path=f"{pulumi_code.name}/{project_source_path}",
    )
    if previous_job:
        passed_job = [previous_job.name]
    else:
        passed_job = None
    aws_creds_path = Output(name=Identifier("aws_creds"))
    pulumi_job_object = Job(
        name=Identifier(f"deploy-{project_name}-{stack_name.lower()}"),
        plan=(dependencies or [])
        + [
            GetStep(
                get=pulumi_code.name,
                trigger=passed_job is None,
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
                        path=f"{pulumi_code.name}/pipelines/infrastructure/scripts/generate_aws_config_from_instance_profile.sh"
                    ),
                ),
            ),
            PutStep(
                put=pulumi_resource.name,
                get_params={"skip_implicit_get": True},
                params={
                    "env_os": {
                        "AWS_DEFAULT_REGION": "us-east-1",
                        "PYTHONPATH": f"/usr/lib/:/tmp/build/put/{pulumi_code.name}/src/",
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
