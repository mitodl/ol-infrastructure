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

ol_data_platform_repo = git_repo(
    name=Identifier("ol-data-platform-repository"),
    uri="https://github.com/mitodl/ol-data-platform",
    branch="main",
    check_every="24h",
    paths=[
        "src/ol_superset/Dockerfile",
        "src/ol_superset/pyproject.toml",
        "src/ol_superset/ol_superset/",
    ],
)

ol_superset_image = Resource(
    name=Identifier("ol-superset-image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "mitodl/ol-superset",
        "tag": "latest",
        "username": "((dockerhub.username))",
        "password": "((dockerhub.password))",
    },
)

docker_pipeline = Pipeline(
    resources=[ol_data_platform_repo, ol_superset_image],
    jobs=[
        Job(
            name=Identifier("build-and-publish-ol-superset-image"),
            plan=[
                GetStep(get=ol_data_platform_repo.name, trigger=True),
                container_build_task(
                    inputs=[Input(name=ol_data_platform_repo.name)],
                    build_parameters={
                        "CONTEXT": f"{ol_data_platform_repo.name}/src/ol_superset",
                        "DOCKERFILE": f"{ol_data_platform_repo.name}/src/ol_superset/Dockerfile",
                    },
                ),
                PutStep(
                    put=ol_superset_image.name,
                    params={
                        "image": "image/image.tar",
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
        "\nfly -t <target> set-pipeline -p ol-superset-image -c definition.json\n"
    )
