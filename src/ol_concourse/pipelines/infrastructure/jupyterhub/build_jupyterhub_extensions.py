from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.models.fragment import PipelineFragment
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
    RegistryImage,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resource_types import pypi_resource
from ol_concourse.lib.resources import git_repo

from ol_concourse.pipelines.pipeline_output import pipeline_json_with_user_data

pypi_type = pypi_resource()
# AI: This pipeline builds and publishes multiple Python packages from a single git
# repository. The build happens via uv package manager (https://docs.astral.sh/uv/)
plugins = [
    "ol-themed-jupyter",
    "ol-jupyter-authoring",
]

fragments = []
for plugin in plugins:
    plugin_git_repo = git_repo(
        Identifier(f"{plugin}-repo"),
        uri="https://github.com/mitodl/ol-notebook-extensions",
        paths=[f"{plugin}"],
        check_every="15m",
    )

    build_job = Job(
        name=Identifier(f"build-{plugin}"),
        plan=[
            GetStep(
                get=plugin_git_repo.name,
                trigger=True,
            ),
            TaskStep(
                task=Identifier("run-build"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type=REGISTRY_IMAGE,
                        source=RegistryImage(
                            repository="ghcr.io/astral-sh/uv", tag="debian-slim"
                        ),
                    ),
                    inputs=[Input(name=plugin_git_repo.name)],
                    outputs=[Output(name=plugin_git_repo.name)],
                    params={
                        "TWINE_USERNAME": "((pypi_creds.username))",
                        "TWINE_PASSWORD": "((pypi_creds.password))",
                    },
                    run=Command(
                        path="sh",
                        args=[
                            "-exc",
                            f"""
                            apt update && apt install -y nodejs npm;
                            cd {plugin_git_repo.name}/{plugin};
                            uv build --package {plugin};
                            uvx twine check dist/*
                            uvx twine upload --skip-existing --non-interactive dist/*
                            """,
                        ],
                    ),
                ),
            ),
        ],
    )
    fragment = PipelineFragment(
        resources=[plugin_git_repo],
        jobs=[build_job],
    )
    fragments.append(fragment)

combined_fragment = PipelineFragment.combine_fragments(*fragments)
pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=combined_fragment.resources,
    jobs=combined_fragment.jobs,
)

if __name__ == "__main__":
    import sys

    output = pipeline_json_with_user_data(
        pipeline,
        user_data={
            "description": (
                "Builds and publishes the `ol-themed-jupyter` and "
                "`ol-jupyter-authoring` Python packages from "
                "mitodl/ol-notebook-extensions to PyPI via `uv build` and "
                "`twine upload` whenever their source changes."
            ),
            "team": "main",
            "category": "package-publishing",
        },
    )
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(output)
    sys.stdout.write(output)
    print()  # noqa: T201
    print("fly -t pr-main sp -p publish-jupyterhub-extensions-pypi -c definition.json")  # noqa: T201
