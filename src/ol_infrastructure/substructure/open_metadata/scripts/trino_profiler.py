"""Trino (Starburst Galaxy) data profiler workflow.

Collects table and column statistics (row count, null %, distinct %, min/max,
mean/std) for all production dbt layer schemas (ol_warehouse_production_*).
Uses a 10% PERCENTAGE sample to keep Starburst query costs manageable.

Runs weekly (Sunday) rather than daily given the size of the lakehouse.
"""

import os

from metadata.workflow.profiler import ProfilerWorkflow

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
                "type": "Profiler",
                "schemaFilterPattern": {
                    "includes": [r"^ol_warehouse_production_.*$"],
                },
                "profileSampleType": "PERCENTAGE",
                "profileSample": 10.0,
                "computeMetrics": True,
                "computeTableMetrics": True,
                "computeColumnMetrics": True,
                # Trino has no system-table metrics implementation in OM
                # (supported only for BigQuery, Snowflake, Redshift).  With
                # useStatistics=True (the default) the profiler attempts a
                # system-table lookup for every table and logs a WARNING:
                #   "No implementation found for trino"
                # Setting False skips that path entirely; all metrics are
                # computed via direct SQL queries, which is the effective
                # behaviour already (system lookup silently falls back).
                "useStatistics": False,
                "includeViews": False,
                "threadCount": 5,
                "timeoutSeconds": 43200,
            }
        },
    },
    "processor": {"type": "orm-profiler", "config": {}},
    "sink": {"type": "metadata-rest", "config": {}},
    "workflowConfig": {
        "openMetadataServerConfig": {
            "hostPort": os.environ["OM_SERVER_URL"],
            "authProvider": "openmetadata",
            "securityConfig": {"jwtToken": os.environ["OM_BOT_JWT_TOKEN"]},
        }
    },
}
workflow = ProfilerWorkflow.create(config)
workflow.execute()
workflow.raise_from_status()
