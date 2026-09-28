import sys

from ol_concourse.lib.containers import container_build_task, ensure_ecr_task
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    Input,
    Job,
    Pipeline,
    PutStep,
    Resource,
)
from ol_concourse.lib.resources import git_repo, registry_image

from ol_concourse.pipelines.constants import ECR_REGION
from ol_concourse.pipelines.ecr import configure_ecr_repository_task
from ol_concourse.pipelines.pipeline_output import pipeline_json_with_user_data

hashicorp_release_repository = git_repo(
    name=Identifier("hashicorp-release-resource"),
    uri="https://github.com/mitodl/hashicorp-release-resource",
    branch="master",
    check_every="24h",
)

hashicorp_release_image = Resource(
    name=Identifier("hashicorp-release-resource-image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "mitodl/hashicorp-release-resource",
        "tag": "latest",
        "password": "((dockerhub.password))",
        "username": "((dockerhub.username))",
    },
)

hashicorp_release_ecr_image = registry_image(
    name=Identifier("hashicorp-release-resource-image-ecr"),
    image_repository="mitodl/hashicorp-release-resource",
    image_tag="latest",
    ecr_region=ECR_REGION,
)

build_task = container_build_task(
    inputs=[Input(name=hashicorp_release_repository.name)],
    build_parameters={"CONTEXT": hashicorp_release_repository.name},
)

docker_pipeline = Pipeline(
    resources=[
        hashicorp_release_repository,
        hashicorp_release_image,
        hashicorp_release_ecr_image,
    ],
    jobs=[
        Job(
            name=Identifier("build-and-publish-container"),
            plan=[
                GetStep(get=hashicorp_release_repository.name, trigger=True),
                build_task,
                ensure_ecr_task("mitodl/hashicorp-release-resource"),
                configure_ecr_repository_task(
                    "mitodl/hashicorp-release-resource", keep_last_n_images=10
                ),
                PutStep(
                    put=hashicorp_release_image.name,
                    params={
                        "image": "image/image.tar",
                        "additional_tags": (
                            f"./{hashicorp_release_repository.name}/.git/describe_ref"
                        ),
                    },
                ),
                PutStep(
                    put=hashicorp_release_ecr_image.name,
                    params={
                        "image": "image/image.tar",
                        "additional_tags": (
                            f"./{hashicorp_release_repository.name}/.git/describe_ref"
                        ),
                    },
                ),
            ],
        )
    ],
)

if __name__ == "__main__":
    output = pipeline_json_with_user_data(
        docker_pipeline,
        user_data={
            "description": (
                "Builds `mitodl/hashicorp-release-resource` from "
                "`mitodl/hashicorp-release-resource`'s `master` branch (24h "
                "poll), a custom Concourse resource type for tracking "
                "HashiCorp product releases (consumed via the "
                "`hashicorp_release()` factory elsewhere in this repo). "
                "Publishes to Docker Hub and ECR."
            ),
            "team": "main",
            "category": "concourse-tooling",
        },
    )
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(output)
    sys.stdout.write(output)
    sys.stdout.write(
        "fly -t pr-main set-pipeline -p docker-hashicorp-release-resource-image -c definition.json"  # noqa: E501
    )
