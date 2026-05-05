"""Trino (Starburst Galaxy) PII auto-classification workflow.

Scans production dbt layer tables (ol_warehouse_production_*) for sensitive
columns using OM's built-in AutoClassificationWorkflow, which combines:

  - ColumnNameScanner: fast regex matching on column names (email, ssn,
    credit card, password, phone, address, date-of-birth, etc.)
  - NERScanner: Presidio NLP analysis on up to 50 sampled rows per column

PII.Sensitive / PII.NonSensitive tags are applied with "Suggested" confidence
so data stewards can review before confirming.  Once confirmed on upstream
tables, OM propagates the tags automatically via lineage to downstream tables.

Sample rows are *not* stored in OM to avoid persisting the PII values the
workflow is trying to detect.

Runs weekly (Sunday) after the profiler job.
"""

import os

from metadata.workflow.classification import AutoClassificationWorkflow

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
                "type": "AutoClassification",
                "schemaFilterPattern": {
                    "includes": [r"^ol_warehouse_production_.*$"],
                },
                "enableAutoClassification": True,
                "storeSampleData": False,
                "sampleDataCount": 50,
                "confidence": 80,
                "includeViews": False,
            }
        },
    },
    "processor": {"type": "tag-pii-processor", "config": {}},
    "sink": {"type": "metadata-rest", "config": {}},
    "workflowConfig": {
        "openMetadataServerConfig": {
            "hostPort": os.environ["OM_SERVER_URL"],
            "authProvider": "openmetadata",
            "securityConfig": {"jwtToken": os.environ["OM_BOT_JWT_TOKEN"]},
        }
    },
}
workflow = AutoClassificationWorkflow.create(config)
workflow.execute()
workflow.raise_from_status()
