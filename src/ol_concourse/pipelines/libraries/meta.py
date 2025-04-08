from ol_concourse.lib.models.pipeline import (
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Output,
    Pipeline,
    Resource,
    SetPipelineStep,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo

# Duplicate the configs here for clarity, or import from api_clients_pipeline.py
# Keeping it duplicated avoids potential import issues if structure changes.
PIPELINE_CONFIGS = {
    "mit_open": {
        "pipeline_name": "open-api-clients",  # Define pipeline name here for set_pipeline # noqa: E501
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
        "publish_script": "open-api-clients-publish-node.sh",  # Uses npm, not yarn
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
        "python_image_tag": "3.11-slim",
        "node_image_tag": "18-slim",
        "publish_script": "api-clients-publish-node.sh",  # Uses yarn
    },
    "unified_ecommerce": {
        "pipeline_name": "unified-ecommerce-api-client",
        "source_repo_name": "unified-ecommerce",
        "source_repo_uri": "https://github.com/mitodl/unified-ecommerce",
        "source_repo_branch": "main",
        "client_repo_name": "unified-ecommerce-api-clients",
        "client_repo_uri": "git@github.com:mitodl/unified-ecommerce-api-clients.git",
        "client_repo_branch": "release",
        "client_repo_subpath": "unified-ecommerce-api-axios",
        "vault_ssh_secret_path": "npm_publish.odlbot_private_ssh_key",  # pragma: allowlist secret  # noqa: E501
        "vault_npm_secret_path": "npm_publish.npmjs_token",  # pragma: allowlist secret  # noqa: E501
        "python_image_tag": "3.11-slim",
        "node_image_tag": "18-slim",
        "publish_script": "api-clients-publish-node.sh",  # Uses yarn
    },
}

# Resource for the ol-concourse code itself
ol_concourse_repo = git_repo(
    name=Identifier("ol-concourse"),
    uri="https://github.com/mitodl/ol-concourse",
    branch="main",
    paths=[
        "src/ol_concourse/pipelines/libraries/",
        "src/ol_concourse/lib/",
    ],
)

# Resource for a Python image to run the generation script
python_image = Resource(
    name=Identifier("python-image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "python",
        "tag": "3.12-slim",
    },  # Use a consistent Python version
)


def build_meta_job(variant: str, config: dict[str, str]) -> Job:
    """Build a job to generate and set a pipeline for a specific variant."""
    pipeline_name = config["pipeline_name"]
    definition_path = f"generated-pipeline/{pipeline_name}-definition.json"
    return Job(
        name=Identifier(f"set-{pipeline_name}-pipeline"),
        plan=[
            GetStep(get=ol_concourse_repo.name, trigger=True),
            GetStep(get=python_image.name),
            TaskStep(
                task=Identifier("generate-pipeline-definition"),
                image=python_image.name,
                config=TaskConfig(
                    platform="linux",
                    inputs=[Input(name=ol_concourse_repo.name)],
                    outputs=[Output(name=Identifier("generated-pipeline"))],
                    run=Command(
                        path="sh",
                        args=[
                            "-exc",
                            f"python {ol_concourse_repo.name}/src/ol_concourse/pipelines/libraries/api_clients_pipeline.py --variant {variant} > {definition_path}",  # noqa: E501
                        ],
                    ),
                ),
            ),
            SetPipelineStep(
                set_pipeline=Identifier(pipeline_name),
                file=definition_path,
            ),
        ],
    )


def meta_pipeline() -> Pipeline:
    """Generate the meta-pipeline for managing API client pipelines."""
    jobs = [
        build_meta_job(variant, config) for variant, config in PIPELINE_CONFIGS.items()
    ]
    return Pipeline(
        resources=[ol_concourse_repo, python_image],
        jobs=jobs,
    )


if __name__ == "__main__":
    import sys

    pipeline = meta_pipeline()
    sys.stdout.write(pipeline.model_dump_json(indent=2))
    print("\nfly -t <target> set-pipeline -p self -c <path/to/this/file>")  # noqa: T201
