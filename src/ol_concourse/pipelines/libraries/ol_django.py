"""Generate a publish pipeline for all packages in the ol-django monorepo."""

from pathlib import Path

from ol_concourse.lib.models.pipeline import Pipeline
from ol_concourse.pipelines.libraries.pypi_monorepo import (
    discover_python_packages,
    monorepo_publish_pipeline,
)

SOURCE_REPO_URI = "https://github.com/mitodl/ol-django"
EXPECTED_ARG_COUNT = 2


def pipeline_from_source(source_repo_path: str | Path) -> tuple[list[str], Pipeline]:
    """Generate the pipeline from a checked-out source repository."""

    package_dirs = discover_python_packages(source_repo_path)
    pipeline = monorepo_publish_pipeline(
        source_repo_uri=SOURCE_REPO_URI,
        package_dirs=package_dirs,
        build_command_factory=lambda package_dir, repo_name: (
            f"""
            cd {repo_name};
            PYTHONPATH=build-support/bin uv build src/{package_dir};
            uvx twine check dist/*
            uvx twine upload --skip-existing --non-interactive dist/*
            """
        ),
    )
    return package_dirs, pipeline


if __name__ == "__main__":
    import sys

    if len(sys.argv) != EXPECTED_ARG_COUNT:
        msg = "Usage: ol_django.py <path-to-ol-django-checkout>"
        raise SystemExit(msg)

    package_dirs, pipeline = pipeline_from_source(sys.argv[1])
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline.model_dump_json(indent=2))
    sys.stdout.write(pipeline.model_dump_json(indent=2))
    sys.stderr.write(
        f"\nDiscovered packages: {', '.join(package_dirs)}\n"
        "fly -t pr-main sp -p publish-ol-django-pypi -c definition.json\n"
    )
