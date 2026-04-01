"""Helpers for building PyPI publish pipelines for Python monorepos."""

import tomllib
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
) -> list[tuple[str, str]]:
    """Discover Python package directories under ``source_root``.

    A package directory is any direct child of ``source_root`` containing a
    ``pyproject.toml`` file.

    Returns:
        Sorted list of ``(dir_name, dist_name)`` tuples where ``dir_name``
        is the filesystem directory name and ``dist_name`` is the
        ``[project] name`` from the package's ``pyproject.toml``.
    """

    package_root = Path(source_repo_path) / source_root
    if not package_root.is_dir():
        msg = f"Package root does not exist: {package_root}"
        raise ValueError(msg)

    packages = []
    for child in sorted(package_root.iterdir()):
        pyproject = child / "pyproject.toml"
        if child.is_dir() and pyproject.is_file():
            with pyproject.open("rb") as f:
                data = tomllib.load(f)
            dist_name = data["project"]["name"]
            packages.append((child.name, dist_name))

    if not packages:
        msg = f"No Python packages found under: {package_root}"
        raise ValueError(msg)
    return packages


def monorepo_publish_pipeline(  # noqa: PLR0913
    *,
    source_repo_uri: str,
    package_dirs: list[tuple[str, str]],
    build_command_factory: Callable[[str, str, str], str],
    source_root: str = "src",
    shared_paths: list[str] | None = None,
    check_every: str = "15m",
) -> Pipeline:
    """Build a publish pipeline with one job per package directory.

    Args:
        package_dirs: List of ``(dir_name, dist_name)`` tuples as returned
            by :func:`discover_python_packages`.
        build_command_factory: Called as ``factory(dir_name, dist_name,
            repo_name)`` and must return a shell command string.
        shared_paths: Repo-root-relative paths (e.g. ``["pyproject.toml",
            "uv.lock"]``) whose changes should trigger every package job in
            addition to the per-package source directory.
    """

    fragments = []
    for dir_name, dist_name in package_dirs:
        repo_resource = git_repo(
            Identifier(f"{dir_name}-repo"),
            uri=source_repo_uri,
            paths=[f"{source_root}/{dir_name}", *(shared_paths or [])],
            check_every=check_every,
        )
        build_job = Job(
            name=Identifier(f"build-{dir_name}"),
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
                                    dir_name, dist_name, str(repo_resource.name)
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
