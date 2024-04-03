import sys

from ol_concourse.lib.models.pipeline import (
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Output,
    Pipeline,
    PutStep,
    Resource,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resource_types import npm_package_resource, semver_resource
from ol_concourse.lib.resources import git_repo, git_semver, npm_package, ssh_git_repo

mit_open_repository_uri = "https://github.com/mitodl/mit-open"
ssh_mit_open_repository_uri = "git@github.com:mitodl/mit-open.git"

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

openapi_clients_semver = git_semver(
    name=Identifier("openapi-clients-semver"),
    uri=ssh_mit_open_repository_uri,
    private_key="((open_api_clients.odlbot_private_ssh_key))",
    branch="main",
    file="openapi/specs/version.yaml",
)

openapi_clients_npm_package = npm_package(
    name=Identifier("openapi-clients-npm-package"),
    package="@mitodl/open-api-axios",
    scope="mitodl",
    npmjs_token="((open_api_clients.npmjs_token))",  # noqa: S106
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
        GetStep(get=mit_open_repository.name, trigger=True),
        GetStep(get=mit_open_api_clients_repository.name, trigger=True),
        GetStep(get=openapi_generator_image.name, trigger=True),
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
        PutStep(
            put=mit_open_api_clients_repository.name,
            params={"repository": mit_open_api_clients_repository.name},
        ),
    ],
)

create_release_job = Job(
    name="create-release",
    plan=[
        GetStep(
            get=mit_open_api_clients_repository.name,
            passed=[generate_clients_job.name],
            trigger=True,
        ),
        PutStep(
            put=openapi_clients_semver.name,
            params={
                "file": "mit-open-api-clients/VERSION",
                "branch": "main",
                "private_key": "((open_api_clients.odlbot_private_ssh_key))",
            },
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
        GetStep(
            get=mit_open_api_clients_repository.name,
            passed=[create_release_job.name],
            trigger=True,
        ),
        PutStep(
            put=openapi_clients_npm_package.name,
            params={
                "path": "mit-open-api-clients/src/typescript/mit-open-api-axios",
            },
        ),
    ],
)

build_pipeline = Pipeline(
    resources=[
        mit_open_repository,
        mit_open_api_clients_repository,
        openapi_generator_image,
        openapi_clients_npm_package,
        openapi_clients_semver,
    ],
    resource_types=[
        semver_resource(),
        npm_package_resource(),
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
