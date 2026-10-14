"""StarRocks metadata ingestion workflow.

Ingests the databases of StarRocks' ``default_catalog``: the tables and
materialized views StarRocks stores itself (e.g. ``b2b_analytics``).  The
connector reads ``information_schema`` of the catalog it connects to, so the
Iceberg tables StarRocks reads through its external catalogs are not ingested
here.  Those are the same tables the Trino and Glue services already hold.

StarRocks has no database/schema split.  OpenMetadata stores each StarRocks
database as a schema under one service-level database, named by
``OM_STARROCKS_DATABASE_NAME``, giving FQNs of the form
``<service>.<database name>.<StarRocks database>.<table>``.

Environment variables
---------------------
OM_SERVICE_NAME              OM database service to write to.
OM_SERVER_URL                OpenMetadata API host:port.
OM_BOT_JWT_TOKEN             Ingestion-bot JWT (from om-ingestion-bot secret).
OM_STARROCKS_HOST_PORT       FE MySQL-protocol endpoint, ``host:port``.
OM_STARROCKS_USERNAME        Vault-issued read-only user.
OM_STARROCKS_PASSWORD        Its password.
OM_STARROCKS_DATABASE_NAME   Name of the service-level database in OM.
"""

import os

from metadata.workflow.metadata import MetadataWorkflow

config = {
    "source": {
        "type": "starrocks",
        "serviceName": os.environ["OM_SERVICE_NAME"],
        "serviceConnection": {
            "config": {
                "type": "StarRocks",
                "hostPort": os.environ["OM_STARROCKS_HOST_PORT"],
                "username": os.environ["OM_STARROCKS_USERNAME"],
                "password": os.environ["OM_STARROCKS_PASSWORD"],
                "databaseName": os.environ["OM_STARROCKS_DATABASE_NAME"],
            }
        },
        "sourceConfig": {"config": {"type": "DatabaseMetadata"}},
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
