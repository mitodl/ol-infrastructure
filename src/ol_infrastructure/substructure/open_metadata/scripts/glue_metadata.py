"""Glue Data Catalog metadata ingestion workflow.

Uses IRSA for Glue API access — no credential secret is needed.

The Glue connector reads all metadata directly from the Glue API (boto3)
without fetching Iceberg metadata files from S3, making it resilient to
stale __dbt_tmp Glue entries left by dbt atomic table swaps.

Compared to the Iceberg connector it additionally provides:
  - locationPath (S3 location) for every table
  - fileFormat (Parquet, ORC, etc.) from SerDe info
  - ExternalTableLineageMixin: lineage edges between tables and their
    S3 storage containers registered in OM
  - Correct table-type classification (Iceberg, External, View, Regular)
  - Schema descriptions from Glue database descriptions
  - supportsDBTExtraction for future dbt workflow enrichment

Schema filter mirrors the iceberg script: only production dbt layer
schemas are ingested (ol_warehouse_{env}_{layer}) to exclude personal
and feature-branch development namespaces.
"""

import os

from metadata.workflow.metadata import MetadataWorkflow

config = {
    "source": {
        "type": "glue",
        "serviceName": os.environ["OM_SERVICE_NAME"],
        "serviceConnection": {
            "config": {
                "type": "Glue",
                "awsConfig": {"awsRegion": os.environ["OM_AWS_REGION"]},
            }
        },
        "sourceConfig": {
            "config": {
                "type": "DatabaseMetadata",
                # Only ingest production dbt layer schemas. Dev/personal schemas
                # follow the pattern ol_warehouse_{env}_{user}_{layer}.
                # Valid schemas are ol_warehouse_{env}_{layer} where env is a
                # single word (production, qa, ci, etc.) and layer is one of
                # the dbt model directory names from ol-data-platform.
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
