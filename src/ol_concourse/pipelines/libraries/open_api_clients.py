import sys
from pathlib import Path

from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    LoadVarStep,
    Output,
    Pipeline,
    PutStep,
    RegistryImage,
    Resource,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo, ssh_git_repo

mit_open_repository_uri = "https://github.com/mitodl/mit-open"


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
        "tag": "3.12-slim",
    },
)
node_image = Resource(
    name=Identifier("node-image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "node",
        "tag": "22-slim",
    },
)

mit_open_repository = git_repo(
    name=Identifier("mit-open"),
    uri=mit_open_repository_uri,
    branch="release",
    paths=["openapi/specs/*.yaml"],
)

mit_open_api_clients_repository = ssh_git_repo(
    name=Identifier("mit-open-api-clients"),
    uri="git@github.com:mitodl/open-api-clients.git",
    branch="main",
    private_key="((open_api_clients.odlbot_private_ssh_key))",
)

generate_clients_job = Job(
    name=Identifier("generate-clients"),
    plan=[
        GetStep(get=python_image.name),
        GetStep(get=mit_open_repository.name, trigger=True),
        GetStep(get=mit_open_api_clients_repository.name, trigger=False),
        GetStep(get=openapi_generator_image.name),
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
                    args=["mit-open-api-clients/scripts/generate-inner.sh"],
                ),
            ),
        ),
        LoadVarStep(
            load_var=Identifier("mitopen-git-rev"),
            file=f"{mit_open_repository.name}/.git/refs/heads/{mit_open_repository.source['branch']}",
            reveal=True,
        ),
        TaskStep(
            task="bump-version",
            image=python_image.name,
            config=TaskConfig(
                platform="linux",
                inputs=[Input(name=mit_open_api_clients_repository.name)],
                outputs=[Output(name=mit_open_api_clients_repository.name)],
                run=Command(
                    path="sh",
                    dir=mit_open_api_clients_repository.name,
                    args=[
                        "-xc",
                        _read_script("open-api-clients-bumpver.sh"),
                    ],
                ),
            ),
        ),
        TaskStep(
            task="commit-changes",
            config=TaskConfig(
                inputs=[Input(name=mit_open_api_clients_repository.name)],
                outputs=[Output(name=mit_open_api_clients_repository.name)],
                image_resource=AnonymousResource(
                    source=RegistryImage(repository="concourse/buildroot", tag="git"),
                    type="registry-image",
                ),
                platform="linux",
                params={"OPEN_REV": "((.:mitopen-git-rev))"},
                run=Command(
                    path="sh",
                    dir=mit_open_api_clients_repository.name,
                    args=["-xc", _read_script("open-api-clients-commit-changes.sh")],
                ),
            ),
        ),
        PutStep(
            put=mit_open_api_clients_repository.name,
            params={
                "repository": mit_open_api_clients_repository.name,
                "tag": "mit-open-api-clients/VERSION",
            },
        ),
    ],
)

publish_job = Job(
    name="publish",
    plan=[
        GetStep(get=node_image.name, trigger=True),
        GetStep(
            get=mit_open_api_clients_repository.name,
            passed=[generate_clients_job.name],
            trigger=True,
        ),
        TaskStep(
            task="publish-node",
            image=node_image.name,
            config=TaskConfig(
                platform="linux",
                inputs=[Input(name=mit_open_api_clients_repository.name)],
                params={"NPM_TOKEN": "((open_api_clients.npmjs_token))"},
                run=Command(
                    path="sh",
                    dir="mit-open-api-clients/src/typescript/mit-open-api-axios",
                    args=["-xc", _read_script("open-api-clients-publish-node.sh")],
                ),
            ),
        ),
    ],
)

build_pipeline = Pipeline(
    resources=[
        mit_open_repository,
        mit_open_api_clients_repository,
        openapi_generator_image,
        python_image,
        node_image,
    ],
    jobs=[
        generate_clients_job,
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
