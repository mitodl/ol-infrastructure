import sys

from ol_concourse.lib.containers import container_build_task
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    Input,
    Job,
    Pipeline,
    PutStep,
    Resource,
)
from ol_concourse.lib.resources import git_repo

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

build_task = container_build_task(
    inputs=[Input(name=hashicorp_release_repository.name)],
    build_parameters={"CONTEXT": hashicorp_release_repository.name},
)

docker_pipeline = Pipeline(
    resources=[hashicorp_release_repository, hashicorp_release_image],
    jobs=[
        Job(
            name=Identifier("build-and-publish-container"),
            plan=[
                GetStep(get=hashicorp_release_repository.name, trigger=True),
                build_task,
                PutStep(
                    put=hashicorp_release_image.name,
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
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(docker_pipeline.model_dump_json(indent=2))
    sys.stdout.write(docker_pipeline.model_dump_json(indent=2))
    sys.stdout.write(
        "fly -t <prod_target> set-pipeline -p docker-hashicorp-release-resource-image -c definition.json"  # noqa: E501
    )
