import sys

from ol_concourse.pipelines.applications.ol_dbt_openmetadata_sync import (
    ol_dbt_openmetadata_sync_pipeline,
    watched_checkout,
)
from ol_concourse.pipelines.pipeline_output import pipeline_json_with_user_data

# Publishes every contract in ol-data-platform's contracts/ to production
# OpenMetadata. dbt bindings (e.g. `dbt_model: dim_user`) resolve against the
# manifest Dagster uploads nightly for OpenMetadata, so a contract merged with a
# brand new model fails here (the sync exits non-zero rather than skipping it)
# until the daily trigger runs after that model is built and ingested. The sync
# stops at the first binding it cannot resolve, so contracts after it in the
# directory wait too. There is no validate step: the OpenMetadata server
# validates every contract daily. A PUT of an unchanged contract changes
# nothing but its updatedAt.
#
# The bot credentials are written to Vault by the open_metadata Pulumi stack
# (secret-concourse/infrastructure/openmetadata_contracts_bot).
sync_pipeline = ol_dbt_openmetadata_sync_pipeline(
    watched_dir="contracts",
    job_name="sync-data-contracts-to-openmetadata",
    task_name="ol-dbt-contracts-sync",
    sync_command=f"""\
uv run --frozen --package ol-dbt-cli ol-dbt contracts sync \\
    --service "Starburst Galaxy" \\
    --manifest /tmp/manifest.json \\
    --contracts-dir {watched_checkout("contracts")}
""",
    bot_credentials="openmetadata_contracts_bot",
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
