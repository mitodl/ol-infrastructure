# src/ol_concourse/pipelines/libraries/api_clients_pipeline.py
# --- START NEW CONTENT ---
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
    vault_ssh_secret_path: str,
    vault_npm_secret_path: str,
    python_image_tag: str = "3.12-slim",
    node_image_tag: str = "22-slim",
    openapi_generator_tag: str = "v7.2.0",
    generate_script: str = "api-clients-generate-inner.sh",
    bump_script: str = "api-clients-bumpver.sh",
    commit_script: str = "api-clients-commit-changes.sh",
    publish_script: str = "api-clients-publish-node.sh",
) -> Pipeline:
    """Generate a pipeline for building and publishing API clients."""
    # Define common resources with parameterized image tags
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
        private_key=f"(({vault_ssh_secret_path}))",
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
                    params={"NPM_TOKEN": f"(({vault_npm_secret_path}))"},
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
    # Configuration for each pipeline variant
    configs = {
        "mit_open": {
            "pipeline_name": "open-api-clients",
            "source_repo_name": "mit-open",
            "source_repo_uri": "https://github.com/mitodl/mit-open",
            "source_repo_branch": "release",
            "client_repo_name": "mit-open-api-clients",
            "client_repo_uri": "git@github.com:mitodl/open-api-clients.git",
            "client_repo_branch": "main",
            "client_repo_subpath": "mit-open-api-axios",
            "vault_ssh_secret_path": "open_api_clients.odlbot_private_ssh_key",  # pragma: allowlist secret  # noqa: E501
            "vault_npm_secret_path": "open_api_clients.npmjs_token",  # pragma: allowlist secret  # noqa: E501
            "python_image_tag": "3.12-slim",
            "node_image_tag": "22-slim",
            # Use the original npm publish script for mit-open
            "publish_script": "open-api-clients-publish-node.sh",
        },
        "mitxonline": {
            "pipeline_name": "mitxonline-api-client",
            "source_repo_name": "mitxonline",
            "source_repo_uri": "https://github.com/mitodl/mitxonline",
            "source_repo_branch": "main",
            "client_repo_name": "mitxonline-api-clients",
            "client_repo_uri": "git@github.com:mitodl/mitxonline-api-clients.git",
            "client_repo_branch": "release",
            "client_repo_subpath": "mitxonline-api-axios",
            "vault_ssh_secret_path": "npm_publish.odlbot_private_ssh_key",  # pragma: allowlist secret  # noqa: E501
            "vault_npm_secret_path": "npm_publish.npmjs_token",  # pragma: allowlist secret  # noqa: E501
            "python_image_tag": "3.11-slim",  # Keep original tags
            "node_image_tag": "18-slim",  # Keep original tags
            "publish_script": "api-clients-publish-node.sh",  # Use generic yarn script
        },
        "unified_ecommerce": {
            "pipeline_name": "unified-ecommerce-api-client",
            "source_repo_name": "unified-ecommerce",
            "source_repo_uri": "https://github.com/mitodl/unified-ecommerce",
            "source_repo_branch": "main",
            "client_repo_name": "unified-ecommerce-api-clients",
            "client_repo_uri": "git@github.com:mitodl/unified-ecommerce-api-clients.git",  # noqa: E501
            "client_repo_branch": "release",
            "client_repo_subpath": "unified-ecommerce-api-axios",
            "vault_ssh_secret_path": "npm_publish.odlbot_private_ssh_key",  # pragma: allowlist secret  # noqa: E501
            "vault_npm_secret_path": "npm_publish.npmjs_token",  # pragma: allowlist secret  # noqa: E501
            "python_image_tag": "3.11-slim",  # Keep original tags
            "node_image_tag": "18-slim",  # Keep original tags
            "publish_script": "api-clients-publish-node.sh",  # Use generic yarn script
        },
    }

    # Generate and print/save pipeline definitions
    for variant, config in configs.items():
        pipeline = generate_api_client_pipeline(**config)
        pipeline_json = pipeline.model_dump_json(indent=2)
        definition_filename = f"{config['pipeline_name']}-definition.json"
        with open(definition_filename, "w") as definition_file:  # noqa: PTH123
            definition_file.write(pipeline_json)
        print(f"Generated {definition_filename}")  # noqa: T201
        # Print command for the first pipeline only for brevity
        if variant == "mit_open":
            sys.stdout.write(pipeline_json)
            sys.stdout.write(
                f"\nfly -t <target> set-pipeline -p {config['pipeline_name']} -c {definition_filename}\n"  # noqa: E501
            )
        else:
            sys.stdout.write(
                f"\nfly -t <target> set-pipeline -p {config['pipeline_name']} -c {definition_filename}\n"  # noqa: E501
            )

# --- END NEW CONTENT ---
