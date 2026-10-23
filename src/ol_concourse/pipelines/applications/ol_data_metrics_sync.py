import sys

from ol_concourse.pipelines.applications.ol_dbt_openmetadata_sync import (
    ol_dbt_openmetadata_sync_pipeline,
)
from ol_concourse.pipelines.pipeline_output import pipeline_json_with_user_data

# Publishes every metric in ol-data-platform's metrics/ to production
# OpenMetadata as a Metric entity, with lineage from the tables named in its
# implemented_by. A metric whose dbt_model is not in the manifest yet, or whose
# description, owners, reviewers or status someone changed in OpenMetadata
# since the bot last wrote it, is reported and left alone, and the build fails
# so the change can become a pull request. A metric that is In Review or
# Rejected in OpenMetadata is also left alone. Other metrics still sync. There
# is no --force here: overriding an edit made in OpenMetadata is a decision for
# a person running the command (ol-data-platform metrics/README.md). An
# unchanged file writes only its lineage edges, which OpenMetadata upserts.
# After an OpenMetadata upgrade every build fails before the first file until
# ol-data-platform commits a refreshed CreateMetric schema snapshot
# (`ol-dbt metrics refresh-schema`).
#
# The bot credentials are written to Vault by the open_metadata substructure
# Pulumi stack (secret-concourse/infrastructure/openmetadata_metrics_bot), for
# metrics-sync-bot, whose name the sync's drift check depends on.
sync_pipeline = ol_dbt_openmetadata_sync_pipeline(
    watched_dir="metrics",
    job_name="sync-metrics-to-openmetadata",
    task_name="ol-dbt-metrics-sync",
    sync_command="""\
uv run --frozen --package ol-dbt-cli ol-dbt metrics sync \\
    --service "Starburst Galaxy" \\
    --manifest /tmp/manifest.json \\
    --metrics-dir ../ol-data-platform-metrics/metrics
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
