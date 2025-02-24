# ruff:  noqa: E501
import sys

from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.containers import container_build_task
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
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resource_types import pulumi_provisioner_resource
from ol_concourse.lib.resources import git_repo, pulumi_provisioner, registry_image


def build_learn_ai_pipeline() -> Pipeline:
    # Used for building a CI docker image and deploying to the CI environment
    learn_ai_main_repo = git_repo(
        Identifier("learn-ai-main"),
        uri="https://github.com/mitodl/learn-ai",
        branch="main",
    )

    # Used for building a release canidate docker image and
    # triggering the deployment to qa
    learn_ai_release_candidate_repo = git_repo(
        Identifier("learn-ai-release-candidate"),
        uri="http://github.com/mitodl/learn-ai",
        branch="release-candidate",
        fetch_tags=True,
        tag_regex=r"v[0-9]\.[0-9]*\.[0-9]",  # examples v0.24.0, v0.26.3
    )

    # Used for trigging the production deployment
    learn_ai_release_repo = git_repo(
        Identifier("learn-ai-release"),
        uri="http://github.com/mitodl/learn-ai",
        branch="release",
        fetch_tags=True,
        tag_regex=r"v[0-9]\.[0-9]*\.[0-9]",  # examples v0.24.0, v0.26.3
    )

    # Used for publishing the CI containers to dockerhub
    learn_ai_registry_ci_image = registry_image(
        name=Identifier("learn-ai-ci-image"),
        image_repository="mitodl/learn-ai-app-main",
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
        tag_regex="[0-9A-Fa-f]+",  # Should only capture the CI images
    )

    # Used for publishing the RC / production containers to dockerhub
    learn_ai_registry_rc_image = registry_image(
        name=Identifier("learn-ai-rc-image"),
        image_repository="mitodl/learn-ai-app-main",
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
        tag_regex=r"v[0-9]\.[0-9]*\.[0-9]",  # examples v0.24.0, v0.26.3
    )

    # Fetches the pulumi code
    ol_infra_repo = git_repo(
        Identifier("ol-infra"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        # Purposely not monitoring paths or using this as a trigger
    )

    # This image is only used for the CI environment
    learn_ai_main_image_build_job = Job(
        name="build-learn-ai-image-from-main",
        build_log_retention={"builds": 10},
        plan=[
            GetStep(get=learn_ai_main_repo.name, trigger=True),
            container_build_task(
                inputs=[Input(name=learn_ai_main_repo.name)],
                build_parameters={
                    "CONTEXT": learn_ai_main_repo.name,
                },
                build_args=[],
            ),
            PutStep(
                put=learn_ai_registry_ci_image.name,
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"./{learn_ai_main_repo.name}/.git/short_ref",
                },
            ),
        ],
    )

    # This image will be used for both the QA and production deployments
    # hopefully it gets a meaningful tag
    learn_ai_release_canidiate_image_build_job = Job(
        name="build-learn-ai-image-from-release-candidate",
        build_log_retention={"builds": 10},
        plan=[
            GetStep(get=learn_ai_release_candidate_repo.name, trigger=True),
            container_build_task(
                inputs=[Input(name=learn_ai_release_candidate_repo.name)],
                build_parameters={
                    "CONTEXT": learn_ai_release_candidate_repo.name,
                },
                build_args=[],
            ),
            PutStep(
                put=learn_ai_registry_rc_image.name,
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"./{learn_ai_release_candidate_repo.name}/.git/ref",  # Should contain a tag if doof is doing his job
                },
            ),
        ],
    )
    learn_ai_main_branch_container_fragement = PipelineFragment(
        resources=[learn_ai_main_repo, learn_ai_registry_ci_image],
        jobs=[learn_ai_main_image_build_job],
    )
    learn_ai_release_candidate_container_fragment = PipelineFragment(
        resources=[
            learn_ai_release_candidate_repo,
            learn_ai_registry_ci_image,
        ],
        jobs=[learn_ai_release_canidiate_image_build_job],
    )

    pulumi_resource_type = pulumi_provisioner_resource()
    pulumi_resource = pulumi_provisioner(
        name=Identifier("pulumi-ol-infrastructure-learn-ai-application"),
        project_name="ol-infrastructure-learn-ai-application",
        project_path=f"{ol_infra_repo.name}/src/ol_infrastructure/applications/learn_ai",
    )

    learn_ai_ci_deployment_job = Job(
        name=Identifier("learn-ai-ci-deployment"),
        max_in_flight=1,
        plan=[
            GetStep(
                get=learn_ai_registry_ci_image.name,
                trigger=True,
                passed=[learn_ai_main_image_build_job.name],
                params={"skip_download": True},
            ),
            GetStep(
                get=ol_infra_repo.name,
                trigger=False,
            ),
            LoadVarStep(
                load_var="image_tag",
                file=f"{learn_ai_registry_ci_image.name}/tag",
            ),
            TaskStep(
                task=Identifier("set-aws-creds"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type=REGISTRY_IMAGE,
                        source=RegistryImage(repository="amazon/aws-cli"),
                    ),
                    inputs=[Input(name=ol_infra_repo.name)],
                    outputs=[Output(name=Identifier("aws_creds"))],
                    run=Command(
                        path=f"{ol_infra_repo.name}/pipelines/infrastructure/scripts/generate_aws_config_from_instance_profile.sh",
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
                            f"/usr/lib/:/tmp/build/put/{ol_infra_repo.name}/src/"
                        ),
                        "GITHUB_TOKEN": "((github.public_repo_access_token))",
                        "LEARN_AI_DOCKER_TAG": "((.:image_tag))",
                    },
                    "stack_name": "applications.learn_ai.CI",
                },
            ),
        ],
    )

    ci_deployment_fragment = PipelineFragment(
        resource_types=[pulumi_resource_type],
        resources=[ol_infra_repo, pulumi_resource, learn_ai_registry_ci_image],
        jobs=[learn_ai_ci_deployment_job],
    )

    learn_ai_rc_deployment_job = Job(
        name=Identifier("learn-ai-rc-deployment"),
        max_in_flight=1,
        plan=[
            GetStep(
                get=learn_ai_registry_rc_image.name,
                trigger=True,
                passed=[learn_ai_release_canidiate_image_build_job.name],
                params={"skip_download": True},
            ),
            GetStep(
                get=ol_infra_repo.name,
                trigger=False,
            ),
            LoadVarStep(
                load_var="image_tag",
                file=f"{learn_ai_registry_rc_image.name}/tag",
            ),
            TaskStep(
                task=Identifier("set-aws-creds"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type=REGISTRY_IMAGE,
                        source=RegistryImage(repository="amazon/aws-cli"),
                    ),
                    inputs=[Input(name=ol_infra_repo.name)],
                    outputs=[Output(name=Identifier("aws_creds"))],
                    run=Command(
                        path=f"{ol_infra_repo.name}/pipelines/infrastructure/scripts/generate_aws_config_from_instance_profile.sh",
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
                            f"/usr/lib/:/tmp/build/put/{ol_infra_repo.name}/src/"
                        ),
                        "GITHUB_TOKEN": "((github.public_repo_access_token))",
                        "LEARN_AI_DOCKER_TAG": "((.:image_tag))",
                    },
                    "stack_name": "applications.learn_ai.QA",
                },
            ),
        ],
    )
    rc_deployment_fragment = PipelineFragment(
        resource_types=[pulumi_resource_type],
        resources=[ol_infra_repo, pulumi_resource, learn_ai_registry_rc_image],
        jobs=[learn_ai_rc_deployment_job],
    )

    learn_ai_production_deployment_job = Job(
        name=Identifier("learn-ai-production-deployment"),
        max_in_flight=1,
        plan=[
            GetStep(get=learn_ai_release_repo.name, trigger=True),
            GetStep(
                get=learn_ai_registry_rc_image.name,
                trigger=False,
                passed=[learn_ai_rc_deployment_job.name],
                params={"skip_download": True},
            ),
            GetStep(
                get=ol_infra_repo.name,
                trigger=False,
            ),
            LoadVarStep(
                load_var="image_tag",
                file=f"{learn_ai_registry_rc_image.name}/tag",
            ),
            TaskStep(
                task=Identifier("set-aws-creds"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type=REGISTRY_IMAGE,
                        source=RegistryImage(repository="amazon/aws-cli"),
                    ),
                    inputs=[Input(name=ol_infra_repo.name)],
                    outputs=[Output(name=Identifier("aws_creds"))],
                    run=Command(
                        path=f"{ol_infra_repo.name}/pipelines/infrastructure/scripts/generate_aws_config_from_instance_profile.sh",
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
                            f"/usr/lib/:/tmp/build/put/{ol_infra_repo.name}/src/"
                        ),
                        "GITHUB_TOKEN": "((github.public_repo_access_token))",
                        "LEARN_AI_DOCKER_TAG": "((.:image_tag))",
                    },
                    "stack_name": "applications.learn_ai.Production",
                },
            ),
        ],
    )
    production_deployment_fragment = PipelineFragment(
        resource_types=[pulumi_resource_type],
        resources=[
            ol_infra_repo,
            pulumi_resource,
            learn_ai_registry_rc_image,
            learn_ai_release_repo,
        ],
        jobs=[learn_ai_production_deployment_job],
    )

    combined_fragment = PipelineFragment.combine_fragments(
        ci_deployment_fragment,
        rc_deployment_fragment,
        production_deployment_fragment,
        learn_ai_main_branch_container_fragement,
        learn_ai_release_candidate_container_fragment,
    )
    return combined_fragment.to_pipeline()


if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(build_learn_ai_pipeline().model_dump_json(indent=2))
    sys.stdout.write(build_learn_ai_pipeline().model_dump_json(indent=2))
    sys.stdout.writelines(
        (
            "\n",
            "fly -t pr-inf sp -p docker-pulumi-learn-ai -c definition.json",
        )
    )
