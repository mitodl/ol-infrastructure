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

concourse_npm_resource_repository = git_repo(
    name=Identifier("mitodl-concourse-npm-resource"),
    uri="https://github.com/mitodl/concourse-npm-resource",
    branch="master",
    check_every="24h",
)

concourse_npm_resource_image = Resource(
    name=Identifier("mitodl-concourse-npm-resource-image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "mitodl/concourse-npm-resource",
        "tag": "latest",
        "password": "((dockerhub.password))",
        "username": "((dockerhub.username))",
    },
)

concourse_npm_resource_ecr_image = registry_image(
    name=Identifier("mitodl-concourse-npm-resource-image-ecr"),
    image_repository="mitodl/concourse-npm-resource",
    image_tag="latest",
    ecr_region=ECR_REGION,
)

build_task = container_build_task(
    inputs=[Input(name=concourse_npm_resource_repository.name)],
    build_parameters={"CONTEXT": concourse_npm_resource_repository.name},
)

docker_pipeline = Pipeline(
    resources=[
        concourse_npm_resource_repository,
        concourse_npm_resource_image,
        concourse_npm_resource_ecr_image,
    ],
    jobs=[
        Job(
            name=Identifier("build-and-publish-container"),
            plan=[
                GetStep(get=concourse_npm_resource_repository.name, trigger=True),
                build_task,
                ensure_ecr_task("mitodl/concourse-npm-resource"),
                PutStep(
                    put=concourse_npm_resource_image.name,
                    params={
                        "image": "image/image.tar",
                        "additional_tags": f"./{concourse_npm_resource_repository.name}/.git/describe_ref",  # noqa: E501
                    },
                ),
                PutStep(
                    put=concourse_npm_resource_ecr_image.name,
                    params={
                        "image": "image/image.tar",
                        "additional_tags": f"./{concourse_npm_resource_repository.name}/.git/describe_ref",  # noqa: E501
                    },
                ),
            ],
        )
    ],
)

if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(docker_pipeline.model_dump_json(indent=2))
    sys.stdout.write(docker_pipeline.model_dump_json(indent=2))
    sys.stdout.write(
        "fly -t <prod_target> set-pipeline -p docker-mitodl-concourse-npm-resource -c definition.json"  # noqa: E501
    )
