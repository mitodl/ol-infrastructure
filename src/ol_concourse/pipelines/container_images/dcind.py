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
from ol_concourse.lib.resources import git_repo, github_release

ol_inf_repo = git_repo(
    name=Identifier("ol-infrastructure-repository"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="md/issue_1881",
    check_every="24h",
    paths=["dockerfiles/dcind/"],
)

earthly_release = github_release(
    name=Identifier("earthly-release-binary"),
    owner="earthly",
    repository="earthly",
    order_by="time",
)

dcind_release_image = Resource(
    name=Identifier("dcind-release-resource-image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "mitodl/dcind",
        "tag": "latest",
        "password": "((dockerhub.password))",
        "username": "((dockerhub.username))",
    },
)

build_task = container_build_task(
    inputs=[Input(name=ol_inf_repo.name)],
    build_parameters={"CONTEXT": f"{ol_inf_repo.name}/dockerfiles/dcind"},
)

docker_pipeline = Pipeline(
    resources=[ol_inf_repo, earthly_release, dcind_release_image],
    jobs=[
        Job(
            name=Identifier("build-and-publish-container"),
            plan=[
                GetStep(get=ol_inf_repo.name, trigger=True),
                GetStep(get=earthly_release.name, trigger=True),
                build_task,
                PutStep(
                    put=dcind_release_image.name,
                    params={
                        "image": "image/image.tar",
                        "additional_tags": (f"./{earthly_release.name}/version"),
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
        "\nfly -t pr-inf set-pipeline -p dcind-resource-image -c definition.json"
    )
