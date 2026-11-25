import sys

from ol_concourse.pipelines.applications.ol_dbt_openmetadata_sync import (
    ol_dbt_openmetadata_sync_pipeline,
    watched_checkout,
)
from ol_concourse.pipelines.pipeline_output import pipeline_json_with_user_data

# Publishes the documents listed in ol-data-platform's articles/ to production
# OpenMetadata as Context Center articles, attached to the tables each one
# describes. The articles' markdown lives elsewhere in that repository, so a
# change to a document alone does not trigger a build; the daily run picks it
# up, after the manifest upload that resolves the tables.
#
# The job also gets the daily time resource, which has no version until its
# first 05:00-06:00 UTC window, so the first build runs in the first window
# after the pipeline is unpaused, not when it is set, and writes to production.
#
# The bot credentials are written to Vault by the open_metadata substructure
# Pulumi stack (secret-concourse/infrastructure/openmetadata_articles_bot), for
# articles-sync-bot.
sync_pipeline = ol_dbt_openmetadata_sync_pipeline(
    watched_dir="articles",
    job_name="sync-articles-to-openmetadata",
    task_name="ol-dbt-articles-sync",
    sync_command=f"""\
uv run --frozen --package ol-dbt-cli ol-dbt articles sync \\
    --service "Starburst Galaxy" \\
    --manifest /tmp/manifest.json \\
    --articles-dir {watched_checkout("articles")}
""",
    bot_credentials="openmetadata_articles_bot",
)

if __name__ == "__main__":
    output = pipeline_json_with_user_data(
        sync_pipeline,
        user_data={
            "description": (
                "Watches `ol-data-platform`'s `articles/` and publishes the "
                "documents it lists to production OpenMetadata as Context "
                "Center articles with `ol-dbt articles sync`, as "
                "articles-sync-bot."
            ),
            "team": "infrastructure",
            "category": "data-platform",
        },
    )
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(output)
    sys.stdout.write(output)
    sys.stdout.write(
        "\nfly -t <target> set-pipeline -p ol-data-articles-sync -c definition.json\n"
    )
