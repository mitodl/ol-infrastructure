import sys
from typing import Any

from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.containers import container_build_task
from ol_concourse.lib.jobs.infrastructure import pulumi_job, pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    LoadVarStep,
    Output,
    Pipeline,
    Platform,
    PutStep,
    RegistryImage,
    Resource,
    ResourceType,
    Step,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resource_types import pulumi_provisioner_resource
from ol_concourse.lib.resources import git_repo, pulumi_provisioner, registry_image
from ol_concourse.pipelines.constants import PULUMI_WATCHED_PATHS


def _define_git_resources(
    app_name: str,
) -> tuple[Resource, Resource, Resource, Resource]:
    """Define the git resources needed for the pipeline."""
    main_repo = git_repo(
        name=Identifier(f"{app_name}-main"),
        uri=f"https://github.com/mitodl/{app_name}",
        branch="main",
    )

    release_candidate_repo = git_repo(
        name=Identifier(f"{app_name}-release-candidate"),
        uri=f"http://github.com/mitodl/{app_name}",
        branch="release-candidate",
        fetch_tags=True,
    )

    release_repo = git_repo(
        name=Identifier(f"{app_name}-release"),
        uri="http://github.com/mitodl/learn-ai",
        branch="release",
        fetch_tags=True,
        tag_regex=r"v[0-9]\.[0-9]*\.[0-9]",  # examples v0.24.0, v0.26.3
    )

    ol_infra_repo = git_repo(
        Identifier(f"ol-infra-{app_name}"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            f"src/ol_infrastructure/applications/{app_name.replace('-', '_')}",
            *PULUMI_WATCHED_PATHS,
        ],
    )
    return (
        main_repo,
        release_candidate_repo,
        release_repo,
        ol_infra_repo,
    )


def _define_registry_image_resources(app_name: str) -> tuple[Resource, Resource]:
    """Define the registry image resources needed for the pipeline."""
    # Used for publishing the CI containers to dockerhub
    registry_ci_image = registry_image(
        name=Identifier(f"{app_name}-ci-image"),
        image_repository=f"mitodl/{app_name}-app-main",
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
        tag_regex="[0-9A-Fa-f]+",  # Should only capture the CI images
        check_every="24h",
    )

    # Used for publishing the RC / production containers to dockerhub
    registry_rc_image = registry_image(
        name=Identifier(f"{app_name}-rc-image"),
        image_repository=f"mitodl/{app_name}-app-rc",
        image_tag=None,  # Only filter on tagged images
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
        tag_regex=r"[0-9]+\.[0-9]+\.[0-9]+",  # examples 0.24.0, 0.26.3
        sort_by_creation=True,
        check_every="24h",
    )

    return (
        registry_ci_image,
        registry_rc_image,
    )


def _define_pulumi_resources(
    app_name: str, ol_infra_repo_name: str
) -> tuple[ResourceType, str]:
    """Define the Pulumi resource type and resource."""
    pulumi_resource_type = pulumi_provisioner_resource()
    pulumi_resource = pulumi_provisioner(
        name=Identifier(f"pulumi-ol-infrastructure-{app_name}-application"),
        project_name=f"ol-infrastructure-{app_name}-application",
        project_path=(
            f"{ol_infra_repo_name}/src/ol_infrastructure/applications/"
            f"{app_name.replace('-', '_')}"
        ),
    )
    return pulumi_resource_type, pulumi_resource


def _build_image_job(
    app_name: Resource,
    branch_type: Resource,
    git_repo_resource: Resource,
    registry_image_resource: Resource,
) -> Job:
    """Generate an image build job for a specific branch type (main or rc)."""
    job_name = f"build-{app_name}-image-from-{branch_type}"
    version_var = f"{branch_type}_version"
    version_output_dir = f"{branch_type}_version"
    version_file = f"{version_output_dir}/version"

    plan = [
        GetStep(get=git_repo_resource.name, trigger=True),
    ]

    # Add version extraction steps only for release_candidate
    if branch_type == "release_candidate":
        plan.extend(
            [
                TaskStep(
                    task=Identifier("fetch-rc-version"),
                    config=TaskConfig(
                        platform=Platform.linux,
                        image_resource=AnonymousResource(
                            type=REGISTRY_IMAGE,
                            source=RegistryImage(repository="alpine"),
                        ),
                        inputs=[Input(name=git_repo_resource.name)],
                        outputs=[Output(name=Identifier(version_output_dir))],
                        run=Command(
                            path="sh",
                            args=[
                                "-c",
                                f"grep 'VERSION = ' {git_repo_resource.name}/main/settings.py | cut -d'\"' -f2 > {version_file}",  # noqa: E501
                            ],
                        ),
                    ),
                ),
                LoadVarStep(
                    load_var=version_var,
                    file=version_file,
                    reveal=True,
                ),
            ]
        )

    plan.extend(
        [
            LoadVarStep(
                load_var="git_ref",
                file=f"{git_repo_resource.name}/.git/ref",
            ),
            container_build_task(
                inputs=[Input(name=git_repo_resource.name)],
                build_parameters={
                    "CONTEXT": git_repo_resource.name,
                    "BUILD_ARG_GIT_REF": "((.:git_ref))",
                },
                build_args=[],
            ),
        ]
    )

    put_params: dict[str, Any] = {"image": "image/image.tar"}
    if branch_type == "main":
        put_params["additional_tags"] = f"./{git_repo_resource.name}/.git/short_ref"
    else:  # release_candidate
        put_params["version"] = f"((.:{version_var}))"
        put_params["bump_aliases"] = True
        put_params["additional_tags"] = f"./{git_repo_resource.name}/.git/ref"

    plan.append(PutStep(put=registry_image_resource.name, params=put_params))

    return Job(name=Identifier(job_name), build_log_retention={"builds": 10}, plan=plan)


def _common_deployment_steps(
    app_name: str,
    stack_env: str,
    registry_image_resource: Resource,
    pulumi_resource: Resource,
    ol_infra_repo: Resource,
) -> list[Step]:
    """Generate the common task and put steps for a deployment job."""
    pulumi_stack_name = f"applications.{app_name.replace('-', '_')}.{stack_env}"
    return [
        LoadVarStep(
            load_var="image_tag",
            file=f"{registry_image_resource.name}/tag",
        ),
        TaskStep(
            task=Identifier("set-aws-creds"),
            config=TaskConfig(
                platform=Platform.linux,
                image_resource=AnonymousResource(
                    type=REGISTRY_IMAGE,
                    source=RegistryImage(repository="amazon/aws-cli"),
                ),
                inputs=[Input(name=ol_infra_repo)],
                outputs=[Output(name=Identifier("aws_creds"))],
                run=Command(
                    path=f"{ol_infra_repo.name}/pipelines/infrastructure/scripts/generate_aws_config_from_instance_profile.sh",
                ),
            ),
        ),
        PutStep(
            put=pulumi_resource,
            get_params={"skip_implicit_get": True},
            params={
                "env_os": {
                    "AWS_DEFAULT_REGION": "us-east-1",
                    "PYTHONPATH": f"/usr/lib/:/tmp/build/put/{ol_infra_repo}/src/",
                    "GITHUB_TOKEN": "((github.public_repo_access_token))",
                    f"{app_name.replace('-', '_').upper()}_DOCKER_TAG": "((.:image_tag))",  # noqa: E501
                },
                "stack_name": pulumi_stack_name,
            },
        ),
    ]


def build_app_pipeline(app_name: str) -> Pipeline:
    # Define Resources
    (
        main_repo,
        release_candidate_repo,
        release_repo,
        ol_infra_repo,
    ) = _define_git_resources(app_name)
    (
        registry_ci_image,
        registry_rc_image,
    ) = _define_registry_image_resources(app_name)
    pulumi_resource_type, pulumi_resource = _define_pulumi_resources(
        app_name, ol_infra_repo.name
    )

    # Define Build Jobs

    main_image_build_job = _build_image_job(
        app_name=app_name,
        branch_type="main",
        git_repo_resource=main_repo,
        registry_image_resource=registry_ci_image,
    )
    rc_image_build_job = _build_image_job(
        app_name=app_name,
        branch_type="release_candidate",
        git_repo_resource=release_candidate_repo,
        registry_image_resource=registry_rc_image,
    )

    # Define Deployment Jobs

    # CI Deployment
    ci_fragment = pulumi_job(
        pulumi_code=ol_infra_repo,
        stack_name=f"applications.{app_name.replace('-', '_')}.CI",
        project_name=f"ol-infrastructure-{app_name}-application",
        project_source_path=(
            f"src/ol_infrastructure/applications/{app_name.replace('-', '_')}"
        ),
        dependencies=[
            GetStep(
                get=registry_ci_image.name,
                trigger=True,
                passed=[main_image_build_job.name],
                params={"skip_download": True},
            ),
        ],
    )

    # QA and Production Deployments
    qa_and_production_fragment = pulumi_jobs_chain(
        pulumi_code=ol_infra_repo,
        stack_names=[
            f"applications.{app_name.replace('-', '_')}.QA",
            f"applications.{app_name.replace('-', '_')}.Production",
        ],
        project_name=f"ol-infrastructure-{app_name}-application",
        project_source_path=(
            f"src/ol_infrastructure/applications/{app_name.replace('-', '_')}"
        ),
        dependencies=[
            GetStep(
                get=registry_rc_image.name,
                trigger=True,
                passed=[rc_image_build_job.name],
                params={"skip_download": True},
            ),
        ],
    )

    # Group into Fragments

    main_branch_container_fragement = PipelineFragment(
        resources=[main_repo, registry_ci_image],
        jobs=[main_image_build_job],
    )

    release_candidate_container_fragment = PipelineFragment(
        resources=[
            release_candidate_repo,
            registry_rc_image,
        ],
        jobs=[rc_image_build_job],
    )

    ci_deployment_fragment = PipelineFragment(
        resource_types=[pulumi_resource_type, *ci_fragment.resource_types],
        resources=[
            ol_infra_repo,
            pulumi_resource,
            registry_ci_image,
            *ci_fragment.resources,
        ],
        jobs=ci_fragment.jobs,
    )

    # Combine all fragments

    combined_fragment = PipelineFragment.combine_fragments(
        ci_deployment_fragment,
        qa_and_production_fragment,
        main_branch_container_fragement,
        release_candidate_container_fragment,
    )
    return combined_fragment.to_pipeline()


if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(
            build_app_pipeline(app_name="learn-ai").model_dump_json(indent=2)
        )
    sys.stdout.write(build_app_pipeline(app_name="learn-ai").model_dump_json(indent=2))
    sys.stdout.writelines(
        (
            "\n",
            "fly -t pr-inf sp -p docker-pulumi-learn-ai -c definition.json",
        )
    )
