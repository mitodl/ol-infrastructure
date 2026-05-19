"""Trino (Starburst Galaxy) metadata ingestion workflow."""

import os

from metadata.workflow.metadata import MetadataWorkflow

config = {
    "source": {
        "type": "trino",
        "serviceName": os.environ["OM_SERVICE_NAME"],
        "serviceConnection": {
            "config": {
                "type": "Trino",
                "hostPort": os.environ["OM_TRINO_HOST_PORT"],
                "username": os.environ["OM_TRINO_USERNAME"],
                "authType": {"password": os.environ["OM_TRINO_PASSWORD"]},
                "catalog": os.environ["OM_TRINO_CATALOG"],
            }
        },
        "sourceConfig": {
            "config": {
                "type": "DatabaseMetadata",
                # Only ingest production dbt layer schemas; excludes system
                # schemas (information_schema, system) and personal dev namespaces
                # (ol_warehouse_{env}_{user}_{layer}).
                "schemaFilterPattern": {
                    "includes": [
                        r"ol_warehouse_[a-z]+_(dimensional|external|intermediate|irx|mart|migration|raw|reporting|staging)$",
                        r"ol_data_lake_[a-z]+",
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
