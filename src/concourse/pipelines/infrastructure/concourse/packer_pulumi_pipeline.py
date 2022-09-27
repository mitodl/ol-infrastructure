from concourse.lib.constants import REGISTRY_IMAGE
from concourse.lib.models.pipeline import (  # noqa: WPS235
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    InParallelStep,
    Input,
    Job,
    Output,
    Pipeline,
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
from concourse.lib.resources import git_repo, github_release, pulumi_provisioner

#########################
# CUSTOM RESOURCE TYPES #
#########################
packer_validate_type = packer_validate()
packer_build_type = packer_build()
pulumi_provisioner_resource_type = pulumi_provisioner_resource()

#############
# RESOURCES #
#############
concourse_release = github_release(
    Identifier("concourse-release"), "concourse", "concourse"
)
concourse_image_code = git_repo(
    Identifier("ol-infrastructure"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/bilder/components/",
        "src/bilder/images/concourse",
        "src/bilder/images/packer.pkr.hcl",
        "src/bilder/images/variables.pkr.hcl",
        "src/bilder/images/config.pkr.hcl",
    ],
)

concourse_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/ol_infrastructure/applications/concourse/",
        "pipelines/infrastructure/scripts/",
    ],
)

packer_validate_resource = Resource(
    name="packer-validate", type=packer_validate_type.name
)

packer_build_resource = Resource(name="packer-build", type=packer_build_type.name)

pulumi_deploy = pulumi_provisioner(
    name=Identifier("pulumi-concourse"),
    project_name="ol-infrastructure-concourse-application",
    project_path=f"{concourse_pulumi_code.name}/src/ol_infrastructure/applications/concourse",  # noqa: E501
)


def ami_jobs() -> list[Job]:
    validate_job = Job(
        name=Identifier("validate-packer-template"),
        plan=[
            GetStep(
                get=concourse_release.name,
                trigger=True,
            ),
            GetStep(get=concourse_image_code.name, trigger=True),
            InParallelStep(
                in_parallel=[
                    PutStep(
                        put=packer_validate_resource.name,
                        params={
                            "template": f"{concourse_image_code.name}/src/bilder/images/.",  # noqa: E501
                            "objective": "validate",
                            "vars": {"app_name": "concourse", "node_type": node_type},
                        },
                    )
                    for node_type in {"web", "worker"}  # noqa: WPS335
                ]
            ),
        ],
    )
    build_job = Job(
        name=Identifier("build-packer-template"),
        plan=[
            GetStep(
                get=concourse_release.name,
                trigger=True,
            ),
            GetStep(
                get=concourse_image_code.name,
                trigger=True,
                passed=[validate_job.name],
            ),
            InParallelStep(
                in_parallel=[
                    PutStep(
                        put=packer_build_resource.name,
                        params={
                            "template": f"{concourse_image_code.name}/src/bilder/images/.",  # noqa: E501
                            "objective": "build",
                            "vars": {"app_name": "concourse", "node_type": node_type},
                            "env_vars": {
                                "AWS_REGION": "us-east-1",
                                "PYTHONPATH": f"${{PYTHONPATH}}:{concourse_image_code.name}/src",  # noqa: E501
                            },
                            "env_vars_from_files": {
                                "CONCOURSE_VERSION": f"{concourse_release.name}/version"
                            },
                            "only": ["amazon-ebs.third-party"],
                        },
                    )
                    for node_type in {"web", "worker"}  # noqa: WPS335
                ]
            ),
        ],
    )
    return [validate_job, build_job]


def pulumi_job(env_stage: str, previous_env_stage: str = None) -> Job:
    if previous_env_stage:
        passed_job = [f"deploy-concourse-{previous_env_stage.lower()}"]
    else:
        passed_job = None
    aws_creds_path = Output(name=Identifier("aws_creds"))
    return Job(
        name=Identifier(f"deploy-concourse-{env_stage.lower()}"),
        plan=[
            # TODO: Pass in the build job step name by reference (TMM 2022-07-15)
            GetStep(
                get=packer_build_resource.name,
                passed=["build-packer-template"],
                trigger=passed_job is None,
            ),
            GetStep(
                get=concourse_pulumi_code.name,
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
                    inputs=[Input(name=concourse_pulumi_code.name)],
                    outputs=[aws_creds_path],
                    run=Command(
                        path=f"{concourse_pulumi_code.name}/pipelines/infrastructure/scripts/generate_aws_config_from_instance_profile.sh"  # noqa: E501
                    ),
                ),
            ),
            PutStep(
                put=pulumi_deploy.name,
                get_params={"skip_implicit_get": True},
                params={
                    "env_os": {
                        "AWS_DEFAULT_REGION": "us-east-1",
                        "PYTHONPATH": f"/usr/lib/:/tmp/build/put/{concourse_pulumi_code.name}/src/",  # noqa: E501
                    },
                    "stack_name": f"applications.concourse.{env_stage}",
                },
            ),
        ],
    )


def concourse_pipeline() -> Pipeline:
    return Pipeline(
        resource_types=[
            packer_validate_type,
            packer_build_type,
            pulumi_provisioner_resource_type,
        ],
        resources=[
            concourse_release,
            concourse_image_code,
            packer_validate_resource,
            packer_build_resource,
            pulumi_deploy,
        ],
        jobs=ami_jobs()
        + [
            pulumi_job(env_stage, previous_env_stage=previous_env)
            for env_stage, previous_env in [  # noqa: WPS335
                ("CI", None),
                ("QA", "CI"),
                ("Production", "QA"),
            ]
        ],
    )


if __name__ == "__main__":
    import sys  # noqa: WPS433

    with open("definition.json", "wt") as definition:
        definition.write(concourse_pipeline().json(indent=2))
    sys.stdout.write(concourse_pipeline().json(indent=2))
    print()  # noqa: WPS421
    print(  # noqa: WPS421
        "fly -t pr-inf sp -p packer-pulumi-concourse -c definition.json"  # noqa: C813
    )
