"""Shared shape of the pipelines that publish an ol-data-platform directory to
production OpenMetadata with an `ol-dbt ... sync` command.
"""

from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Pipeline,
    Platform,
    RegistryImage,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo, schedule

# Downloads the manifest Dagster uploads nightly for OpenMetadata to
# /tmp/manifest.json, which resolves dbt_model bindings to production relations.
DOWNLOAD_MANIFEST = """\
uv run --frozen --package ol-dbt-cli python -c '
import boto3
boto3.client("s3").download_file(
    "dagster-data-production",
    "openmetadata/dbt-artifacts/manifest.json",
    "/tmp/manifest.json",
)
'
"""


def watched_resource_name(watched_dir: str) -> str:
    """Name the path-filtered git resource that watches `watched_dir`.

    :param watched_dir: The ol-data-platform directory, e.g. ``contracts``.
    :returns: The resource name, which is also its input directory in the task.
    :rtype: str
    """
    return f"ol-data-platform-{watched_dir}"


def watched_checkout(watched_dir: str) -> str:
    """Locate `watched_dir` in its own checkout, relative to the repository.

    :param watched_dir: The ol-data-platform directory, e.g. ``contracts``.
    :returns: The path to pass to the sync command, which runs from the
        unfiltered repository checkout.
    :rtype: str
    """
    return f"../{watched_resource_name(watched_dir)}/{watched_dir}"


def ol_dbt_openmetadata_sync_pipeline(
    *,
    watched_dir: str,
    job_name: str,
    task_name: str,
    sync_command: str,
    bot_credentials: str,
) -> Pipeline:
    """Build a pipeline that runs `sync_command` when `watched_dir` changes and
    daily after the manifest upload.

    :param watched_dir: The ol-data-platform directory whose commits trigger a
        sync, e.g. ``contracts``. ``watched_checkout`` gives its path for the
        sync command.
    :param job_name: The Concourse job name.
    :param task_name: The Concourse task name.
    :param sync_command: The shell command run from the repository checkout
        after the manifest is downloaded to /tmp/manifest.json.
    :param bot_credentials: The Vault key under secret-concourse/infrastructure/
        holding the bot's ``server_url`` and ``token``.
    :returns: The pipeline.
    :rtype: Pipeline
    """
    # A commit that changes the watched directory triggers a sync, and its files
    # are read from this resource's checkout, which is always the commit that
    # last changed them. ol-dbt-cli runs from the unfiltered checkout below: a
    # path-filtered resource only has versions for commits under its paths, so
    # its CLI code and uv.lock would be as old as the last commit there.
    # (Reading the files from the unfiltered checkout instead could miss the
    # triggering commit, since the two resources are checked independently.)
    watched_changes = git_repo(
        name=Identifier(watched_resource_name(watched_dir)),
        uri="https://github.com/mitodl/ol-data-platform",
        branch="main",
        check_every="60s",
        paths=[f"{watched_dir}/"],
    )

    ol_data_platform_repo = git_repo(
        name=Identifier("ol-data-platform-repository"),
        uri="https://github.com/mitodl/ol-data-platform",
        branch="main",
        check_every="60s",
    )

    # Also daily, after the 03:00 UTC om-ingest-dbt run and the 04:00 UTC
    # manifest upload, so a file that failed because its model was new succeeds
    # once the model is in both.
    daily_sync = schedule(
        Identifier("daily-after-manifest"), start="05:00", stop="06:00"
    )

    return Pipeline(
        resources=[watched_changes, ol_data_platform_repo, daily_sync],
        jobs=[
            Job(
                name=Identifier(job_name),
                # Each build publishes its own checkout, so two overlapping
                # builds (a merge during the daily run) could let the older one
                # finish last and put stale files back.
                serial=True,
                plan=[
                    GetStep(get=watched_changes.name, trigger=True),
                    GetStep(get=daily_sync.name, trigger=True),
                    GetStep(get=ol_data_platform_repo.name),
                    TaskStep(
                        task=Identifier(task_name),
                        config=TaskConfig(
                            platform=Platform.linux,
                            # Not the debian-slim tag: ol-dbt finds the repo root
                            # with `git rev-parse`, and slim has no git.
                            image_resource=AnonymousResource(
                                type="registry-image",
                                source=RegistryImage(
                                    repository="ghcr.io/astral-sh/uv",
                                    tag="python3.13-bookworm",
                                ),
                            ),
                            inputs=[
                                Input(name=watched_changes.name),
                                Input(name=ol_data_platform_repo.name),
                            ],
                            params={
                                "AWS_DEFAULT_REGION": "us-east-1",
                                "OM_SERVER_URL": f"(({bot_credentials}.server_url))",
                                "OM_BOT_JWT_TOKEN": f"(({bot_credentials}.token))",
                            },
                            run=Command(
                                path="bash",
                                args=[
                                    "-c",
                                    "set -euo pipefail\n"
                                    "cd ol-data-platform-repository\n"
                                    f"{DOWNLOAD_MANIFEST}{sync_command}",
                                ],
                            ),
                        ),
                    ),
                ],
            )
        ],
    )
