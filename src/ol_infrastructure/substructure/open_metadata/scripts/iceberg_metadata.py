"""Iceberg (Glue + S3) metadata ingestion workflow.

Uses IRSA for Glue/S3 access — no credential secret is needed.

Note: the Iceberg connector does not support lineage extraction
(supportsLineageExtraction is absent from its schema). Lineage for
Iceberg-backed tables is captured by the Trino lineage workflow, since
Trino is the query engine that reads and writes these tables.
"""

import os

from metadata.workflow.metadata import MetadataWorkflow

config = {
    "source": {
        "type": "iceberg",
        "serviceName": os.environ["OM_SERVICE_NAME"],
        "serviceConnection": {
            "config": {
                "type": "Iceberg",
                "catalog": {
                    "name": os.environ["OM_SERVICE_NAME"],
                    "connection": {
                        "awsConfig": {"awsRegion": os.environ["OM_AWS_REGION"]},
                    },
                },
            }
        },
        "sourceConfig": {
            "config": {
                "type": "DatabaseMetadata",
                # Only ingest production dbt layer schemas. Dev/personal schemas
                # follow the pattern ol_warehouse_{env}_{user}_{layer} and
                # have stale __dbt_tmp Glue entries pointing to non-existent S3
                # paths. Valid schemas are ol_warehouse_{env}_{layer} where env
                # is a single word (production, qa, ci, etc.) and layer is one
                # of the dbt model directory names from ol-data-platform.
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
