# src/ol_concourse/pipelines/libraries/api_clients_pipeline.py
import argparse
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
from ol_concourse.pipelines.libraries.configuration import PIPELINE_CONFIGS


def _read_script(script_name: str) -> str:
    """Read a script file from the scripts directory."""
    return (Path(__file__).parent / "scripts" / script_name).read_text()


def generate_api_client_pipeline(  # noqa: PLR0913
    source_repo_name: str,
    source_repo_uri: str,
    source_repo_branch: str,
    client_repo_name: str,
    client_repo_uri: str,
    client_repo_branch: str,
    client_repo_subpath: str,
) -> Pipeline:
    """
    Generate a pipeline definition for building and publishing API clients.

    :param source_repo_name: The identifier for the source code repository resource.
    :param source_repo_uri: The URI of the source code repository (e.g., GitHub URL).
    :param source_repo_branch: The branch of the source code repository to track.
    :param client_repo_name: The identifier for the generated client code repository
        resource.
    :param client_repo_uri: The URI of the client code repository (e.g., GitHub SSH
        URL).
    :param client_repo_branch: The branch of the client code repository to push to.
    :param client_repo_subpath: The subpath within the client repo where the generated
        code resides (relative to src/typescript/).

    :return: A Pipeline object representing the Concourse pipeline definition.
    """
    # Define parameterized image tags
    python_image_tag = "3.12-slim"
    node_image_tag = "24-slim"
    openapi_generator_tag = "v7.2.0"

    # Define script names
    generate_script: str = "generate-inner.sh"
    bump_script: str = "api-clients-bumpver.sh"
    commit_script: str = "api-clients-commit-changes.sh"
    publish_script: str = "api-clients-publish-node.sh"

    openapi_generator_image = Resource(
        name=Identifier("openapi-generator-image"),
        type="registry-image",
        icon="docker",
        source={
            "repository": "openapitools/openapi-generator-cli",
            "tag": openapi_generator_tag,
        },
    )
    python_image = Resource(
        name=Identifier("python-image"),
        type="registry-image",
        icon="docker",
        source={
            "repository": "python",
            "tag": python_image_tag,
        },
    )
    node_image = Resource(
        name=Identifier("node-image"),
        type="registry-image",
        icon="docker",
        source={
            "repository": "node",
            "tag": node_image_tag,
        },
    )

    # Define source and client repositories using parameters
    source_repository = git_repo(
        name=Identifier(source_repo_name),
        uri=source_repo_uri,
        branch=source_repo_branch,
        paths=["openapi/specs/*.yaml"],
    )

    api_clients_repository = ssh_git_repo(
        name=Identifier(client_repo_name),
        uri=client_repo_uri,
        branch=client_repo_branch,
        private_key="((npm_publish.odlbot_private_ssh_key))",
    )

    # Define the 'generate-clients' job
    generate_clients_job = Job(
        name=Identifier("generate-clients"),
        plan=[
            GetStep(get=python_image.name),
            GetStep(get=source_repository.name, trigger=True),
            GetStep(get=api_clients_repository.name, trigger=False),
            GetStep(get=openapi_generator_image.name),
            TaskStep(
                task=Identifier("generate-apis"),
                image=openapi_generator_image.name,
                config=TaskConfig(
                    platform="linux",
                    inputs=[
                        Input(name=source_repository.name),
                        Input(name=api_clients_repository.name),
                    ],
                    outputs=[Output(name=api_clients_repository.name)],
                    run=Command(
                        path="/bin/bash",
                        args=[
                            f"{api_clients_repository.name}/scripts/{generate_script}"
                        ],
                    ),
                ),
            ),
            LoadVarStep(
                load_var=Identifier(f"{source_repo_name}-git-rev"),
                file=f"{source_repository.name}/.git/refs/heads/{source_repository.source['branch']}",
                reveal=True,
            ),
            TaskStep(
                task="bump-version",
                image=python_image.name,
                config=TaskConfig(
                    platform="linux",
                    inputs=[Input(name=api_clients_repository.name)],
                    outputs=[Output(name=api_clients_repository.name)],
                    run=Command(
                        path="sh",
                        dir=api_clients_repository.name,
                        args=[
                            "-xc",
                            _read_script(bump_script),
                        ],
                    ),
                ),
            ),
            TaskStep(
                task="commit-changes",
                config=TaskConfig(
                    inputs=[Input(name=api_clients_repository.name)],
                    outputs=[Output(name=api_clients_repository.name)],
                    image_resource=AnonymousResource(
                        source=RegistryImage(
                            repository="concourse/buildroot", tag="git"
                        ),
                        type="registry-image",
                    ),
                    platform="linux",
                    params={
                        "SOURCE_REPO_NAME": source_repo_name,
                        "OPEN_REV": f"((.:{source_repo_name}-git-rev))",
                    },
                    run=Command(
                        path="sh",
                        dir=api_clients_repository.name,
                        args=["-xc", _read_script(commit_script)],
                    ),
                ),
            ),
            PutStep(
                put=api_clients_repository.name,
                params={
                    "repository": api_clients_repository.name,
                    "tag": f"{api_clients_repository.name}/VERSION",
                },
            ),
        ],
    )

    # Define the 'publish' job
    publish_job = Job(
        name="publish",
        plan=[
            GetStep(get=node_image.name, trigger=True),
            GetStep(
                get=api_clients_repository.name,
                passed=[generate_clients_job.name],
                trigger=True,
            ),
            TaskStep(
                task="publish-node",
                image=node_image.name,
                config=TaskConfig(
                    platform="linux",
                    inputs=[Input(name=api_clients_repository.name)],
                    params={"NPM_TOKEN": "((npm_publish.npmjs_token))"},
                    run=Command(
                        path="sh",
                        # Adjust dir based on which publish script is used
                        dir=f"{api_clients_repository.name}/src/typescript/{client_repo_subpath}",
                        args=["-xc", _read_script(publish_script)],
                    ),
                ),
            ),
        ],
    )

    # Construct the final pipeline
    return Pipeline(
        resources=[
            source_repository,
            api_clients_repository,
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
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--variant",
        required=True,
        choices=PIPELINE_CONFIGS.keys(),
        help="The pipeline variant configuration to generate.",
    )
    args = parser.parse_args()

    variant_config = PIPELINE_CONFIGS[args.variant]
    pipeline = generate_api_client_pipeline(**variant_config)
    # Print the generated pipeline definition JSON to stdout
    sys.stdout.write(pipeline.model_dump_json(indent=2))
