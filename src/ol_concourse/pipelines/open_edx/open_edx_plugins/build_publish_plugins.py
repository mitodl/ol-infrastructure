"""Generate a publish pipeline for all packages in the open-edx-plugins repo."""

from pathlib import Path

from ol_concourse.lib.models.pipeline import Pipeline
from ol_concourse.pipelines.libraries.pypi_monorepo import (
    discover_python_packages,
    monorepo_publish_pipeline,
)

SOURCE_REPO_URI = "https://github.com/mitodl/open-edx-plugins"
EXPECTED_ARG_COUNT = 2


def pipeline_from_source(source_repo_path: str | Path) -> tuple[list[str], Pipeline]:
    """Generate the pipeline from a checked-out source repository."""

    plugins = discover_python_packages(source_repo_path)
    pipeline = monorepo_publish_pipeline(
        source_repo_uri=SOURCE_REPO_URI,
        package_dirs=plugins,
        build_command_factory=lambda plugin, repo_name: (
            f"""
            cd {repo_name};
            uv build --package {plugin};
            uvx twine check dist/*
            uvx twine upload --skip-existing --non-interactive dist/*
            """
        ),
    )
    return plugins, pipeline


if __name__ == "__main__":
    import sys

    if len(sys.argv) != EXPECTED_ARG_COUNT:
        msg = "Usage: build_publish_plugins.py <path-to-open-edx-plugins-checkout>"
        raise SystemExit(msg)

    plugins, pipeline = pipeline_from_source(sys.argv[1])
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline.model_dump_json(indent=2))
    sys.stdout.write(pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print(f"Discovered packages: {', '.join(plugins)}")  # noqa: T201
    print("fly -t pr-main sp -p publish-open-edx-plugins-pypi -c definition.json")  # noqa: T201
