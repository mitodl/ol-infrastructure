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
# Unlike the other syncs, this one reads its files from the unfiltered
# checkout: the path-filtered one stays at the last commit under articles/, so
# its copy of a document edited since is stale. The sync reads each article's
# markdown from the checkout its articles/ is in, so the files come from the
# path-filtered checkout only when the unfiltered one has not caught up with
# the commit that triggered the build (git merge-base fails on a commit the
# unfiltered clone does not have yet).
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
watched={watched_checkout("articles")}
articles_dir=articles
if ! git merge-base --is-ancestor "$(git -C "$watched" rev-parse HEAD)" HEAD; then
    articles_dir=$watched
fi
uv run --frozen --package ol-dbt-cli ol-dbt articles sync \\
    --service "Starburst Galaxy" \\
    --manifest /tmp/manifest.json \\
    --articles-dir "$articles_dir"
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
