import sys

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

from ol_concourse.pipelines.pipeline_output import pipeline_json_with_user_data

# A commit that changes contracts/ triggers a sync, and the contracts are read
# from this resource's checkout, which is always the commit that last changed
# them. ol-dbt-cli runs from the unfiltered checkout below: a path-filtered
# resource only has versions for commits under its paths, so its CLI code and
# uv.lock would be as old as the last contracts/ commit. (Reading the contracts
# from the unfiltered checkout instead could miss the triggering commit, since
# the two resources are checked independently.)
contracts_changes = git_repo(
    name=Identifier("ol-data-platform-contracts"),
    uri="https://github.com/mitodl/ol-data-platform",
    branch="main",
    check_every="60s",
    paths=["contracts/"],
)

ol_data_platform_repo = git_repo(
    name=Identifier("ol-data-platform-repository"),
    uri="https://github.com/mitodl/ol-data-platform",
    branch="main",
    check_every="60s",
)

# Also daily, after the 03:00 UTC om-ingest-dbt run and the 04:00 UTC manifest
# upload, so a contract that failed because its model was new succeeds once the
# model is in both. A PUT of an unchanged contract changes nothing but its
# updatedAt.
daily_sync = schedule(Identifier("daily-after-manifest"), start="05:00", stop="06:00")

# Publishes every contract in ol-data-platform's contracts/ to production
# OpenMetadata. dbt bindings (e.g. `dbt_model: dim_user`) resolve against the
# manifest Dagster uploads nightly for OpenMetadata, so a contract merged with a
# brand new model fails here (the sync exits non-zero rather than skipping it)
# until the daily trigger runs after that model is built and ingested. The sync
# stops at the first binding it cannot resolve, so contracts after it in the
# directory wait too. There is no validate step: the OpenMetadata server
# validates every contract daily.
#
# The bot credentials are written to Vault by the open_metadata Pulumi stack
# (secret-concourse/infrastructure/openmetadata_contracts_bot).
_sync_script = """\
set -euo pipefail
cd ol-data-platform-repository
uv run --frozen --package ol-dbt-cli python -c '
import boto3
boto3.client("s3").download_file(
    "dagster-data-production",
    "openmetadata/dbt-artifacts/manifest.json",
    "/tmp/manifest.json",
)
'
uv run --frozen --package ol-dbt-cli ol-dbt contracts sync \\
    --service "Starburst Galaxy" \\
    --manifest /tmp/manifest.json \\
    --contracts-dir ../ol-data-platform-contracts/contracts
"""

sync_pipeline = Pipeline(
    resources=[contracts_changes, ol_data_platform_repo, daily_sync],
    jobs=[
        Job(
            name=Identifier("sync-data-contracts-to-openmetadata"),
            # Each build publishes its own checkout, so two overlapping builds
            # (a merge during the daily run) could let the older one finish last
            # and put stale contracts back.
            serial=True,
            plan=[
                GetStep(get=contracts_changes.name, trigger=True),
                GetStep(get=daily_sync.name, trigger=True),
                GetStep(get=ol_data_platform_repo.name),
                TaskStep(
                    task=Identifier("ol-dbt-contracts-sync"),
                    config=TaskConfig(
                        platform=Platform.linux,
                        # Not the debian-slim tag: `ol-dbt contracts` finds the
                        # repo root with `git rev-parse`, and slim has no git.
                        image_resource=AnonymousResource(
                            type="registry-image",
                            source=RegistryImage(
                                repository="ghcr.io/astral-sh/uv",
                                tag="python3.13-bookworm",
                            ),
                        ),
                        inputs=[
                            Input(name=contracts_changes.name),
                            Input(name=ol_data_platform_repo.name),
                        ],
                        params={
                            "AWS_DEFAULT_REGION": "us-east-1",
                            "OM_SERVER_URL": "((openmetadata_contracts_bot.server_url))",  # noqa: E501
                            "OM_BOT_JWT_TOKEN": "((openmetadata_contracts_bot.token))",
                        },
                        run=Command(path="bash", args=["-c", _sync_script]),
                    ),
                ),
            ],
        )
    ],
)

if __name__ == "__main__":
    output = pipeline_json_with_user_data(
        sync_pipeline,
        user_data={
            "description": (
                "Watches `ol-data-platform`'s `contracts/` and publishes each "
                "data contract to production OpenMetadata with "
                "`ol-dbt contracts sync`, as the OpenMetadata ingestion bot."
            ),
            "team": "infrastructure",
            "category": "data-platform",
        },
    )
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(output)
    sys.stdout.write(output)
    sys.stdout.write(
        "\nfly -t <target> set-pipeline -p ol-data-contracts-sync -c definition.json\n"
    )
