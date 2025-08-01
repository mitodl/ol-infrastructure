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

pypi_type = pypi_resource()
# AI: This pipeline builds and publishes multiple Python packages from a single git
# repository. The build happens via uv package manager (https://docs.astral.sh/uv/)
plugins = [
    "edx_sysadmin",
    "edx_username_changer",
    "ol_openedx_canvas_integration",
    "ol_openedx_chat",
    "ol_openedx_chat_xblock",
    "ol_openedx_checkout_external",
    "ol_openedx_course_export",
    "ol_openedx_course_structure_api",
    "ol_openedx_course_sync",
    "ol_openedx_git_auto_export",
    "ol_openedx_logging",
    "ol_openedx_otel_monitoring",
    "ol_openedx_rapid_response_reports",
    "ol_openedx_sentry",
    "ol_social_auth",
    "openedx_companion_auth",
    "rapid_response_xblock",
]

fragments = []
for plugin in plugins:
    plugin_git_repo = git_repo(
        Identifier(f"{plugin}-repo"),
        uri="https://github.com/mitodl/open-edx-plugins",
        paths=[f"src/{plugin}"],
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
                            cd {plugin_git_repo.name};
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

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline.model_dump_json(indent=2))
    sys.stdout.write(pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-main sp -p publish-open-edx-plugins-pypi -c definition.json")  # noqa: T201
