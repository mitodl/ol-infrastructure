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

herokuconfigurator_release_repository = git_repo(
    name=Identifier("herokuconfigurator-release-resource"),
    uri="https://github.com/mitodl/herokuconfigurator",
    branch="heroku_configurator",
    check_every="24h",
)

herokuconfigurator_release_image = Resource(
    name=Identifier("herokuconfigurator-image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "mitodl/herokuconfigurator",
        "tag": "latest",
        "password": "((dockerhub.password))",
        "username": "((dockerhub.username))",
    },
)

build_task = container_build_task(
    inputs=[Input(name=herokuconfigurator_release_repository.name)],
    build_parameters={"CONTEXT": herokuconfigurator_release_repository.name},
)

docker_pipeline = Pipeline(
    resources=[herokuconfigurator_release_repository, herokuconfigurator_release_image],
    jobs=[
        Job(
            name=Identifier("build-and-publish-container"),
            plan=[
                GetStep(get=herokuconfigurator_release_repository.name, trigger=True),
                build_task,
                PutStep(
                    put=herokuconfigurator_release_image.name,
                    params={
                        "image": "image/image.tar",
                        "additional_tags": f"./{herokuconfigurator_release_repository.name}/.git/describe_ref",  # noqa: E501
                    },
                ),
            ],
        )
    ],
)

if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(docker_pipeline.json(indent=2))
    sys.stdout.write(docker_pipeline.json(indent=2))
    sys.stdout.write(
        "fly -t <prod_target> set-pipeline -p docker-herokuconfigurator-image -c definition.json"  # noqa: E501
    )
