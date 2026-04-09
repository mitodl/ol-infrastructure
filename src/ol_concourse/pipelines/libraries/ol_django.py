"""Generate a release pipeline for all packages in the ol-django monorepo."""

from pathlib import Path

from ol_concourse.lib.models.pipeline import Pipeline
from ol_concourse.pipelines.libraries.pypi_monorepo import (
    discover_python_packages,
    monorepo_publish_pipeline,
)

SOURCE_REPO_URI = "https://github.com/mitodl/ol-django"
SOURCE_REPO_SSH_URI = "git@github.com:mitodl/ol-django.git"
EXPECTED_ARG_COUNT = 2


def pipeline_from_source(
    source_repo_path: str | Path,
) -> tuple[list[tuple[str, str]], Pipeline]:
    """Generate the pipeline from a checked-out source repository."""

    packages = discover_python_packages(source_repo_path)
    pipeline = monorepo_publish_pipeline(
        source_repo_uri=SOURCE_REPO_URI,
        package_dirs=packages,
        shared_paths=["pyproject.toml", "uv.lock"],
        task_params={
            "GIT_SSH_KEY": "((github.odlbot_private_ssh_key))",
            "GIT_USER_NAME": "odl-bot",
            "GIT_USER_EMAIL": "odl-devops@mit.edu",
        },
        build_command_factory=lambda dir_name, dist_name, repo_name: (
            f"""
            cd {repo_name}

            # Skip if this is already a release commit to avoid re-triggering
            last_msg=$(git log -1 --format=%s)
            if echo "$last_msg" | grep -qE '^Release .+/v'; then
                echo "Skipping: already a release commit"
                exit 0
            fi

            # Install git and openssh (not present in debian-slim base image)
            apt-get update -qq
            apt-get install -y -q --no-install-recommends git openssh-client

            # Configure git identity for the release commit
            git config --global user.name "$GIT_USER_NAME"
            git config --global user.email "$GIT_USER_EMAIL"

            # Configure SSH so the release script can push to GitHub
            mkdir -p ~/.ssh
            echo "$GIT_SSH_KEY" > ~/.ssh/id_ed25519
            chmod 600 ~/.ssh/id_ed25519
            ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null

            # Switch the remote to SSH for push access
            git remote set-url origin {SOURCE_REPO_SSH_URI}

            # Run the full release process for this package:
            #   - validates that changelog.d/ entries exist
            #   - bumps the version (date-based with incremental builds)
            #   - merges changelog.d/ entries into CHANGELOG.md
            #   - commits and tags the release ({dist_name}/v{{version}})
            #   - pushes the commit and tag, which triggers GitHub Actions to
            #     build and publish the package to PyPI
            uv run scripts/release.py create --app {dir_name} --push
            """
        ),
    )
    return packages, pipeline


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
        f"\nDiscovered packages: {', '.join(dist for _, dist in package_dirs)}\n"
        "fly -t pr-main sp -p publish-ol-django-pypi -c definition.json\n"
    )
