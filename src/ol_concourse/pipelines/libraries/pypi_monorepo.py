"""Helpers for building PyPI publish pipelines for Python monorepos."""

from collections.abc import Callable
from pathlib import Path

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
from ol_concourse.lib.resources import git_repo


def discover_python_packages(
    source_repo_path: str | Path, source_root: str = "src"
) -> list[str]:
    """Discover Python package directories under ``source_root``.

    A package directory is any direct child of ``source_root`` containing a
    ``pyproject.toml`` file.
    """

    package_root = Path(source_repo_path) / source_root
    if not package_root.is_dir():
        msg = f"Package root does not exist: {package_root}"
        raise ValueError(msg)

    package_dirs = sorted(
        child.name
        for child in package_root.iterdir()
        if child.is_dir() and (child / "pyproject.toml").is_file()
    )
    if not package_dirs:
        msg = f"No Python packages found under: {package_root}"
        raise ValueError(msg)
    return package_dirs


def monorepo_publish_pipeline(
    *,
    source_repo_uri: str,
    package_dirs: list[str],
    build_command_factory: Callable[[str, str], str],
    source_root: str = "src",
    check_every: str = "15m",
) -> Pipeline:
    """Build a publish pipeline with one job per package directory."""

    fragments = []
    for package_dir in package_dirs:
        repo_resource = git_repo(
            Identifier(f"{package_dir}-repo"),
            uri=source_repo_uri,
            paths=[f"{source_root}/{package_dir}"],
            check_every=check_every,
        )
        build_job = Job(
            name=Identifier(f"build-{package_dir}"),
            plan=[
                GetStep(
                    get=repo_resource.name,
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
                        inputs=[Input(name=repo_resource.name)],
                        outputs=[Output(name=repo_resource.name)],
                        params={
                            "TWINE_USERNAME": "((pypi_creds.username))",
                            "TWINE_PASSWORD": "((pypi_creds.password))",
                        },
                        run=Command(
                            path="sh",
                            args=[
                                "-exc",
                                build_command_factory(
                                    package_dir, str(repo_resource.name)
                                ),
                            ],
                        ),
                    ),
                ),
            ],
        )
        fragments.append(
            PipelineFragment(
                resources=[repo_resource],
                jobs=[build_job],
            )
        )

    combined_fragment = PipelineFragment.combine_fragments(*fragments)
    return Pipeline(
        resource_types=combined_fragment.resource_types,
        resources=combined_fragment.resources,
        jobs=combined_fragment.jobs,
    )
