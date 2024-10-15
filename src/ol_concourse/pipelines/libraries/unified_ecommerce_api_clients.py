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

unified_ecommerce_repository_uri = "https://github.com/mitodl/unified-ecommerce"


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

unified_ecommerce_repository = git_repo(
    name=Identifier("unified-ecommerce"),
    uri=unified_ecommerce_repository_uri,
    branch="release",
    paths=["openapi/specs/*.yaml"],
)

unified_ecommerce_api_clients_repository = ssh_git_repo(
    name=Identifier("unified-ecommerce-api-clients"),
    uri="git@github.com:mitodl/open-api-clients.git",
    branch="main",
    private_key="((unified_ecommerce_api_clients.odlbot_private_ssh_key))",
)

generate_clients_job = Job(
    name=Identifier("generate-clients"),
    plan=[
        GetStep(get=python_image.name),
        GetStep(get=unified_ecommerce_repository.name, trigger=True),
        GetStep(get=unified_ecommerce_api_clients_repository.name, trigger=False),
        GetStep(get=openapi_generator_image.name),
        TaskStep(
            task=Identifier("generate-apis"),
            image=openapi_generator_image.name,
            config=TaskConfig(
                platform="linux",
                inputs=[
                    Input(name=unified_ecommerce_repository.name),
                    Input(name=unified_ecommerce_api_clients_repository.name),
                ],
                outputs=[Output(name=unified_ecommerce_api_clients_repository.name)],
                run=Command(
                    path="/bin/bash",
                    args=["unified-ecommerce-api-clients/scripts/generate-inner.sh"],
                ),
            ),
        ),
        LoadVarStep(
            load_var=Identifier("unified-ecommerce-git-rev"),
            file=f"{unified_ecommerce_repository.name}/.git/refs/heads/{unified_ecommerce_repository.source['branch']}",
            reveal=True,
        ),
        TaskStep(
            task="bump-version",
            image=python_image.name,
            config=TaskConfig(
                platform="linux",
                inputs=[Input(name=unified_ecommerce_api_clients_repository.name)],
                outputs=[Output(name=unified_ecommerce_api_clients_repository.name)],
                run=Command(
                    path="sh",
                    dir=unified_ecommerce_api_clients_repository.name,
                    args=[
                        "-xc",
                        _read_script("unified-ecommerce-api-clients-bumpver.sh"),
                    ],
                ),
            ),
        ),
        TaskStep(
            task="commit-changes",
            config=TaskConfig(
                inputs=[Input(name=unified_ecommerce_api_clients_repository.name)],
                outputs=[Output(name=unified_ecommerce_api_clients_repository.name)],
                image_resource=AnonymousResource(
                    source=RegistryImage(repository="concourse/buildroot", tag="git"),
                    type="registry-image",
                ),
                platform="linux",
                params={"OPEN_REV": "((.:unified-ecommerce-git-rev))"},
                run=Command(
                    path="sh",
                    dir=unified_ecommerce_api_clients_repository.name,
                    args=["-xc", _read_script("unified-ecommerce-api-clients-commit-changes.sh")],
                ),
            ),
        ),
        PutStep(
            put=unified_ecommerce_api_clients_repository.name,
            params={
                "repository": unified_ecommerce_api_clients_repository.name,
                "tag": "unified-ecommerce-api-clients/VERSION",
            },
        ),
    ],
)

publish_job = Job(
    name="publish",
    plan=[
        GetStep(get=node_image.name, trigger=True),
        GetStep(
            get=unified_ecommerce_api_clients_repository.name,
            passed=[generate_clients_job.name],
            trigger=True,
        ),
        TaskStep(
            task="publish-node",
            image=node_image.name,
            config=TaskConfig(
                platform="linux",
                inputs=[Input(name=unified_ecommerce_api_clients_repository.name)],
                params={"NPM_TOKEN": "((unified_ecommerce_api_clients.npmjs_token))"},
                run=Command(
                    path="sh",
                    dir="unified-ecommerce-api-clients/src/typescript/unified-ecommerce-api-axios",
                    args=["-xc", _read_script("unified-ecommerce-api-clients-publish-node.sh")],
                ),
            ),
        ),
    ],
)

build_pipeline = Pipeline(
    resources=[
        unified_ecommerce_repository,
        unified_ecommerce_api_clients_repository,
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
