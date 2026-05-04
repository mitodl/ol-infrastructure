"""Superset (PostgreSQL/RDS IAM auth) metadata ingestion workflow.

Connects to the Superset RDS instance as the Vault-managed read_only_role
database user using IAM authentication. No password secret is needed —
the ingestion service account's IRSA role grants rds-db:connect, and
GRANT rds_iam TO "read_only_role" (added in vault.py) enables IAM auth
for Vault-issued readonly credentials.
"""

import os

from metadata.workflow.metadata import MetadataWorkflow

config = {
    "source": {
        "type": "superset",
        "serviceName": os.environ["OM_SERVICE_NAME"],
        "serviceConnection": {
            "config": {
                "type": "Superset",
                "connection": {
                    "type": "PostgresConnection",
                    "username": "read_only_role",
                    "hostPort": f"{os.environ['OM_SUPERSET_DB_HOST']}:5432",
                    "database": "superset",
                    "authType": {
                        "awsConfig": {
                            "awsRegion": os.environ["OM_AWS_REGION"],
                            "enabled": True,
                        },
                    },
                },
            }
        },
        "sourceConfig": {"config": {"type": "DashboardMetadata"}},
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
