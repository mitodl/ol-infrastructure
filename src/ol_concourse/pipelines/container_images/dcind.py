import sys

from ol_concourse.lib.containers import container_build_task
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Output,
    Pipeline,
    Platform,
    PutStep,
    Resource,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo, github_release

ol_inf_repo = git_repo(
    name=Identifier("ol-infrastructure-repository"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
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


docker_pipeline = Pipeline(
    resources=[ol_inf_repo, earthly_release, dcind_release_image],
    jobs=[
        Job(
            name=Identifier("build-and-publish-container"),
            plan=[
                GetStep(get=ol_inf_repo.name, trigger=True),
                GetStep(get=earthly_release.name, trigger=True),
                TaskStep(
                    task=Identifier("collect-earthly-version"),
                    config=TaskConfig(
                        platform=Platform.linux,
                        image_resource=AnonymousResource(
                            type="registry-image",
                            source={
                                "repository": "alpine",
                                "tag": "3",
                            },
                        ),
                        inputs=[Input(name=earthly_release.name)],
                        outputs=[
                            Output(name=Identifier("earthly-version")),
                        ],
                        run=Command(
                            path="sh",
                            args=[
                                "-xc",
                                f"""echo "EARTHLY_VERSION=$(cat {earthly_release.name}/tag)" > earthly-version/args_file;""",  # noqa: E501
                            ],
                        ),
                    ),
                ),
                container_build_task(
                    inputs=[
                        Input(name=ol_inf_repo.name),
                        Input(name="earthly-version"),
                    ],
                    build_parameters={
                        "CONTEXT": f"{ol_inf_repo.name}/dockerfiles/dcind",
                        "BUILD_ARGS_FILE": "earthly-version/args_file",
                    },
                ),
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
