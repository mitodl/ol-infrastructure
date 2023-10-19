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

build_task = container_build_task(
    inputs=[Input(name=tubular_repository.name)],
    build_parameters={"CONTEXT": tubular_repository.name},
)

docker_pipeline = Pipeline(
    resources=[tubular_repository, tubular_image],
    jobs=[
        Job(
            name=Identifier("build-and-publish-container"),
            plan=[
                GetStep(get=tubular_repository.name, trigger=True),
                build_task,
                PutStep(
                    put=tubular_image.name,
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
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(docker_pipeline.model_dump_json(indent=2))
    sys.stdout.write(docker_pipeline.model_dump_json(indent=2))
    sys.stdout.write(
        "fly -t <prod_target> set-pipeline -p docker-openedx-tubular-image -c definition.json"  # noqa: E501
    )
