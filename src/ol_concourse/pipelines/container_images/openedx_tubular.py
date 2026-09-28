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

tubular_repository = git_repo(
    name=Identifier("openedx-tubular"),
    uri="https://github.com/mitodl/tubular",
    branch="cpatti_openedx_tubular",
    check_every="24h",
)

tubular_image = Resource(
    name=Identifier("openedx-tubular-image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "mitodl/openedx-tubular",
        "tag": "latest",
        "password": "((dockerhub.password))",
        "username": "((dockerhub.username))",
    },
)

tubular_ecr_image = registry_image(
    name=Identifier("openedx-tubular-image-ecr"),
    image_repository="mitodl/openedx-tubular",
    image_tag="latest",
    ecr_region=ECR_REGION,
)

build_task = container_build_task(
    inputs=[Input(name=tubular_repository.name)],
    build_parameters={"CONTEXT": tubular_repository.name},
)

docker_pipeline = Pipeline(
    resources=[tubular_repository, tubular_image, tubular_ecr_image],
    jobs=[
        Job(
            name=Identifier("build-and-publish-container"),
            plan=[
                GetStep(get=tubular_repository.name, trigger=True),
                build_task,
                ensure_ecr_task("mitodl/openedx-tubular"),
                configure_ecr_repository_task(
                    "mitodl/openedx-tubular", keep_last_n_images=10
                ),
                PutStep(
                    put=tubular_image.name,
                    params={
                        "image": "image/image.tar",
                        "additional_tags": (
                            f"./{tubular_repository.name}/.git/describe_ref"
                        ),
                    },
                ),
                PutStep(
                    put=tubular_ecr_image.name,
                    params={
                        "image": "image/image.tar",
                        "additional_tags": (
                            f"./{tubular_repository.name}/.git/describe_ref"
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
                "Builds `mitodl/openedx-tubular` from "
                "`mitodl/tubular`'s `cpatti_openedx_tubular` branch (24h "
                "poll, no path filter). Publishes to Docker Hub and ECR."
            ),
            "team": "main",
            "category": "base-images",
        },
    )
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(output)
    sys.stdout.write(output)
    sys.stdout.write(
        "fly -t pr-main set-pipeline -p docker-openedx-tubular-image -c definition.json"
    )
