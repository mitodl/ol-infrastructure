import sys
from pathlib import Path

from ol_concourse.lib.models.pipeline import (
    Command,
    GetStep,
    Identifier,
    InParallelStep,
    Input,
    Job,
    Output,
    Pipeline,
    PutStep,
    Resource,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo, ssh_git_repo


def _read_script(script_name: str) -> str:
    return (Path(__file__).parent / "scripts" / script_name).read_text()


git_image = Resource(
    name=Identifier("git-image"),
    type="docker-image",
    source={
        "repository": "concourse/buildroot",
        "tag": "git",
    },
)
openapi_generator_image = Resource(
    name=Identifier("openapi-generator-image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "openapitools/openapi-generator-cli",
        "tag": "v7.2.0",
    },
)
python_image = Resource(
    name=Identifier("python-image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "python",
        "tag": "3.11-slim",
    },
)
node_image = Resource(
    name=Identifier("node-image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "node",
        "tag": "18-slim",
    },
)

mit_open_repository = git_repo(
    name=Identifier("mit-open"),
    uri="https://github.com/mitodl/mit-open",
    branch="release",
    paths=["openapi/specs/*.yaml"],
)
mit_open_api_clients_repository = ssh_git_repo(
    name=Identifier("mit-open-api-clients"),
    uri="git@github.com/mitodl/open-api-clients.git",
    branch="main",
    private_key="((git-private-key))",
)

generate_clients_job = Job(
    name=Identifier("generate-clients"),
    plan=[
        InParallelStep(
            in_parallel=[
                GetStep(get=git_image.name),
                GetStep(get=openapi_generator_image.name),
                GetStep(get=mit_open_repository.name, trigger=True),
                GetStep(get=mit_open_api_clients_repository.name, trigger=True),
            ]
        ),
        TaskStep(
            task=Identifier("generate-apis"),
            image=openapi_generator_image.name,
            config=TaskConfig(
                platform="linux",
                inputs=[
                    Input(name=mit_open_repository.name),
                    Input(name=mit_open_api_clients_repository.name),
                ],
                outputs=[Output(name=mit_open_api_clients_repository.name)],
                run=Command(
                    path="/bin/bash",
                    args=["open-api-clients/scripts/generate-inner.sh"],
                ),
            ),
        ),
        TaskStep(
            task="git-commit",
            image=git_image.name,
            config=TaskConfig(
                platform="linux",
                inputs=[
                    Input(name=mit_open_repository.name),
                    Input(name=mit_open_api_clients_repository.name),
                ],
                outputs=[Output(name=mit_open_api_clients_repository.name)],
                run=Command(
                    path="/bin/bash",
                    args=[
                        "-exc",
                        _read_script("open-api-clients-commit-changes.sh"),
                    ],
                ),
            ),
        ),
        PutStep(
            put=mit_open_api_clients_repository.name,
            params={"repository": mit_open_api_clients_repository.name},
        ),
    ],
)

create_release_job = Job(
    name="create-release",
    plan=[
        InParallelStep(
            in_parallel=[
                GetStep(get=git_image.name),
                GetStep(get=python_image.name),
                GetStep(
                    get=mit_open_api_clients_repository.name,
                    passed=[generate_clients_job.name],
                    trigger=True,
                ),
            ]
        ),
        TaskStep(
            task="bump-version",
            image=python_image.name,
            config=TaskConfig(
                platform="linux",
                inputs=[Input(name=mit_open_api_clients_repository.name)],
                outputs=[Output(name=mit_open_api_clients_repository.name)],
                run=Command(
                    path="/bin/bash",
                    args=["-exc", _read_script("open-api-clients-bumpver.sh")],
                ),
            ),
        ),
        TaskStep(
            task="git-commit-and-tag",
            image=git_image.name,
            config=TaskConfig(
                platform="linux",
                inputs=[
                    Input(name=mit_open_repository.name),
                    Input(name=mit_open_api_clients_repository.name),
                ],
                outputs=[Output(name=mit_open_api_clients_repository.name)],
                run=Command(
                    path="/bin/bash",
                    args=[
                        "-exc",
                        _read_script("open-api-clients-tag-release.sh"),
                    ],
                ),
            ),
        ),
        PutStep(
            put=mit_open_api_clients_repository.name,
            params={"repository": mit_open_api_clients_repository.name},
        ),
    ],
)

publish_job = Job(
    name="publish",
    plan=[
        InParallelStep(
            in_parallel=[
                GetStep(get=node_image.name),
                GetStep(
                    get=mit_open_api_clients_repository.name,
                    passed=[create_release_job.name],
                    trigger=True,
                ),
            ]
        ),
        TaskStep(
            task="publish-node",
            image=node_image.name,
            config=TaskConfig(
                platform="linux",
                inputs=[Input(name=mit_open_api_clients_repository.name)],
                params={"NPM_TOKEN": "((npm.auth_token))"},
                run=Command(
                    path="/bin/bash",
                    dir="open-api-clients/src/typescript/mit-open-api-axios",
                    args=["-exc", _read_script("open-api-clients-publish-node.sh")],
                ),
            ),
        ),
    ],
)

build_pipeline = Pipeline(
    resources=[
        git_image,
        openapi_generator_image,
        python_image,
        node_image,
        mit_open_repository,
        mit_open_api_clients_repository,
    ],
    jobs=[
        generate_clients_job,
        create_release_job,
        publish_job,
    ],
)

if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(build_pipeline.model_dump_json(indent=2))
    sys.stdout.write(build_pipeline.model_dump_json(indent=2))
    sys.stdout.write(
        "fly -t <prod_target> set-pipeline -p open-api-clients -c definition.json"
    )