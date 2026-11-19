import sys

from ol_concourse.pipelines.applications.ol_dbt_openmetadata_sync import (
    ol_dbt_openmetadata_sync_pipeline,
    watched_checkout,
)
from ol_concourse.pipelines.pipeline_output import pipeline_json_with_user_data

# Publishes the business glossary declared in ol-data-platform's glossary/ to
# production OpenMetadata, which the om_server_config Job in the open_metadata
# substructure stack did until the glossary moved there. The glossary binds no
# dbt models, so the build skips the manifest download the other two syncs do.
#
# The glossary's reviewers are the data-platform team and glossary-sync-bot is
# not in it, so a term the sync creates or changes waits In Review for that
# team in OpenMetadata, and the bot cannot approve it.
#
# The job also gets the daily time resource, which has no version until its
# first 05:00-06:00 UTC window, so the first build runs in the first window
# after the pipeline is unpaused, not when it is set, and writes to production.
# It needs the substructure stack to have created the bot's secret first.
#
# The bot credentials are written to Vault by the open_metadata substructure
# Pulumi stack (secret-concourse/infrastructure/openmetadata_glossary_bot), for
# glossary-sync-bot.
sync_pipeline = ol_dbt_openmetadata_sync_pipeline(
    watched_dir="glossary",
    job_name="sync-glossary-to-openmetadata",
    task_name="ol-dbt-glossary-sync",
    sync_command=f"""\
uv run --frozen --package ol-dbt-cli ol-dbt glossary sync \\
    --glossary-dir {watched_checkout("glossary")}
""",
    bot_credentials="openmetadata_glossary_bot",
    download_manifest=False,
)

if __name__ == "__main__":
    output = pipeline_json_with_user_data(
        sync_pipeline,
        user_data={
            "description": (
                "Watches `ol-data-platform`'s `glossary/` and publishes the "
                "business glossary to production OpenMetadata with "
                "`ol-dbt glossary sync`, as glossary-sync-bot."
            ),
            "team": "infrastructure",
            "category": "data-platform",
        },
    )
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(output)
    sys.stdout.write(output)
    sys.stdout.write(
        "\nfly -t <target> set-pipeline -p ol-data-glossary-sync -c definition.json\n"
    )
