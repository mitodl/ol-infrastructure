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

dagger_release = github_release(
    name=Identifier("dagger-release-binary"),
    owner="dagger",
    repository="dagger",
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
    resources=[ol_inf_repo, dagger_release, dcind_release_image],
    jobs=[
        Job(
            name=Identifier("build-and-publish-container"),
            plan=[
                GetStep(get=ol_inf_repo.name, trigger=True),
                GetStep(get=dagger_release.name, trigger=True),
                TaskStep(
                    task=Identifier("collect-dagger-version"),
                    config=TaskConfig(
                        platform=Platform.linux,
                        image_resource=AnonymousResource(
                            type="registry-image",
                            source={
                                "repository": "alpine",
                                "tag": "3",
                            },
                        ),
                        inputs=[Input(name=dagger_release.name)],
                        outputs=[
                            Output(name=Identifier("dagger-version")),
                        ],
                        run=Command(
                            path="sh",
                            args=[
                                "-xc",
                                f"""echo "DAGGER_VERSION=$(cat {dagger_release.name}/tag | grep -Eo '[0-9]+\\.[0-9]+\\.[0-9]+')" > dagger-version/args_file;
                                echo "$(cat {dagger_release.name}/tag | grep -Eo '[0-9]+\\.[0-9]+\\.[0-9]+')" > dagger-version/tag_file;
                                """,  # noqa: E501
                            ],
                        ),
                    ),
                ),
                container_build_task(
                    inputs=[
                        Input(name=ol_inf_repo.name),
                        Input(name="dagger-version"),
                    ],
                    build_parameters={
                        "CONTEXT": f"{ol_inf_repo.name}/dockerfiles/dcind",
                        "BUILD_ARGS_FILE": "dagger-version/args_file",
                    },
                ),
                PutStep(
                    put=dcind_release_image.name,
                    inputs="detect",
                    params={
                        "image": "image/image.tar",
                        "additional_tags": ("./dagger-version/tag_file"),
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
