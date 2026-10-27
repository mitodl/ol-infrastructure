import sys

from ol_concourse.pipelines.applications.ol_dbt_openmetadata_sync import (
    ol_dbt_openmetadata_sync_pipeline,
    watched_checkout,
)
from ol_concourse.pipelines.pipeline_output import pipeline_json_with_user_data

# Publishes every metric in ol-data-platform's metrics/ to production
# OpenMetadata as a Metric entity, with lineage from each implemented_by
# entity. An implemented_by model tagged starrocks links from its view in the
# StarRocks service (`--starrocks-service`, default "StarRocks", the
# substructure stack's service_name_starrocks default). A view OpenMetadata
# has not catalogued yet is skipped with a warning; the build still passes.
#
# Each of these is reported, leaves that metric unwritten and fails the build,
# while the other metrics still sync:
# - a dbt_model not in the manifest yet (the daily run picks it up once it is);
# - a description, owners, reviewers or status someone changed in OpenMetadata
#   since the bot last wrote it, which belongs in a pull request
#   (ol-data-platform metrics/README.md). There is no --force here, so the
#   pipeline never overwrites such an edit;
# - a metric that is In Review or Rejected in OpenMetadata, so a review in
#   progress keeps the build red until a reviewer acts.
# A displayName that differs between the file and OpenMetadata also fails the
# build, as `stale`, but the rest of that metric still syncs: OpenMetadata
# keeps a stored one against any bot's change, so a person has to set it there.
# An unchanged file writes only its lineage edges, which OpenMetadata upserts.
#
# After an OpenMetadata upgrade to a new major or minor version every build
# fails before the first file until the CreateMetric schema snapshot in
# ol-data-platform is refreshed to it (`ol-dbt metrics refresh-schema`). That
# commit is outside metrics/, so the pipeline recovers at the next daily run or
# a manual trigger. A patch upgrade only prints a `version` warning in a green
# build's log, so the snapshot can fall a patch release behind unnoticed.
#
# The job also gets the daily time resource, which has no version until its
# first 05:00-06:00 UTC window, so the first build runs in the first window
# after the pipeline is unpaused, not when it is set, and writes to production.
# It needs the substructure stack to have created the bot's secret first.
#
# The bot credentials are written to Vault by the open_metadata substructure
# Pulumi stack (secret-concourse/infrastructure/openmetadata_metrics_bot), for
# metrics-sync-bot, whose name the sync's drift check depends on.
sync_pipeline = ol_dbt_openmetadata_sync_pipeline(
    watched_dir="metrics",
    job_name="sync-metrics-to-openmetadata",
    task_name="ol-dbt-metrics-sync",
    sync_command=f"""\
uv run --frozen --package ol-dbt-cli ol-dbt metrics sync \\
    --service "Starburst Galaxy" \\
    --manifest /tmp/manifest.json \\
    --metrics-dir {watched_checkout("metrics")}
""",
    bot_credentials="openmetadata_metrics_bot",
)

if __name__ == "__main__":
    output = pipeline_json_with_user_data(
        sync_pipeline,
        user_data={
            "description": (
                "Watches `ol-data-platform`'s `metrics/` and publishes each "
                "metric to production OpenMetadata with `ol-dbt metrics sync`, "
                "as metrics-sync-bot."
            ),
            "team": "infrastructure",
            "category": "data-platform",
        },
    )
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(output)
    sys.stdout.write(output)
    sys.stdout.write(
        "\nfly -t <target> set-pipeline -p ol-data-metrics-sync -c definition.json\n"
    )
