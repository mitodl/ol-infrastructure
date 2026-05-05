"""dbt artifact metadata enrichment workflow for OpenMetadata.

Downloads manifest.json, catalog.json, and run_results.json from the
Dagster S3 bucket (uploaded by DbtS3ArtifactsResource after each full
dbt build in the lakehouse code location) and enriches the existing
Glue service tables in OpenMetadata with:
  - Model and column descriptions from dbt YAML docs
  - dbt model tags (stored under the "dbtTags" classification)
  - Test results (dbt test outcomes surfaced as OM test cases)
  - dbt lineage (model-to-model dependencies and source → model edges)

The serviceName must match the database service that owns the tables
being enriched (typically the Glue service). dbt model FQNs of the form
``database.schema.model_name`` are resolved against tables in that
service. Tables that have no matching dbt node are left untouched.

IRSA provides ambient S3 and AWS credentials — no credential secret
is needed.

Environment variables
---------------------
OM_SERVICE_NAME       Name of the OM database service to enrich (Glue).
OM_SERVER_URL         OpenMetadata API host:port.
OM_BOT_JWT_TOKEN      Ingestion-bot JWT (from om-ingestion-bot secret).
OM_AWS_REGION         AWS region for the S3 client.
OM_DBT_BUCKET         S3 bucket written by DbtS3ArtifactsResource.
OM_DBT_PREFIX         Key prefix (default: openmetadata/dbt-artifacts).
"""

import os

from metadata.workflow.metadata import MetadataWorkflow

config = {
    "source": {
        "type": "dbt",
        # Must match the existing OM database service whose tables will be
        # enriched with dbt metadata (the Glue service).
        "serviceName": os.environ["OM_SERVICE_NAME"],
        "sourceConfig": {
            "config": {
                "type": "DBT",
                "dbtConfigSource": {
                    "dbtConfigType": "s3",
                    # IRSA provides ambient credentials; only the region is needed.
                    "dbtSecurityConfig": {
                        "awsRegion": os.environ["OM_AWS_REGION"],
                    },
                    "dbtPrefixConfig": {
                        "dbtBucketName": os.environ["OM_DBT_BUCKET"],
                        "dbtObjectPrefix": os.environ.get(
                            "OM_DBT_PREFIX", "openmetadata/dbt-artifacts"
                        ),
                    },
                },
                "dbtUpdateDescriptions": True,
                "dbtUpdateOwners": False,
                "includeTags": True,
                # Match only production dbt layer schemas (same filter as glue job)
                # to avoid attempting to resolve dev-namespace model names.
                "schemaFilterPattern": {
                    "includes": [
                        "ol_warehouse_[a-z]+_(dimensional|external|intermediate|mart|migration|reporting|staging)$",
                        "ol_data_lake_[a-z]+",
                    ],
                },
            }
        },
    },
    "sink": {"type": "metadata-rest", "config": {}},
    "workflowConfig": {
        "openMetadataServerConfig": {
            "hostPort": os.environ["OM_SERVER_URL"],
            "authProvider": "openmetadata",
            "securityConfig": {"jwtToken": os.environ["OM_BOT_JWT_TOKEN"]},
        }
    },
}

workflow = MetadataWorkflow.create(config)
workflow.execute()
workflow.raise_from_status()
