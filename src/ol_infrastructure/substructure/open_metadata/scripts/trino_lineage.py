"""Trino (Starburst Galaxy) lineage extraction workflow.

Starburst Galaxy's 30-day query history lives in
galaxy_telemetry.public.query_history, which uses different column names
than the standard Trino system.runtime.queries view that OM's built-in
TrinoLineageSource expects:

  OM expected         Galaxy telemetry
  ──────────────────  ─────────────────────
  "query"             query          (same)
  "user"              email
  "started"           create_time
  "end"               end_time
  "state" = FINISHED  query_state = COMPLETED

We patch sql_stmt and filters directly on TrinoLineageSource before
constructing the workflow, so all of OM's SQL parsing, entity resolution,
and lineage posting logic remains intact. Only the query against the history
table is replaced.

Lineage for Iceberg-backed tables is also captured here since Trino is the
query engine that reads and writes them.
"""

import os
import textwrap

from metadata.ingestion.source.database.trino.lineage import TrinoLineageSource
from metadata.workflow.lineage import LineageWorkflow

TrinoLineageSource.sql_stmt = textwrap.dedent(
    """
    select query as query_text,
      email as user_name,
      create_time as start_time,
      end_time as end_time
    from galaxy_telemetry.public.query_history
    WHERE query NOT LIKE '/* {{"app": "OpenMetadata", %%}} */%%'
    AND query NOT LIKE '/* {{"app": "dbt", %%}} */%%'
    AND CAST(create_time AS date) >= date_parse('{start_time}', '%Y-%m-%d %H:%i:%s')
    AND CAST(create_time AS date) < date_parse('{end_time}', '%Y-%m-%d %H:%i:%s')
    AND query_state = 'COMPLETED'
    {filters}
    LIMIT {result_limit}
    """
)
TrinoLineageSource.filters = """
    AND (
        lower(query) LIKE '%%create%%table%%as%%select%%'
        OR lower(query) LIKE '%%insert%%into%%select%%'
        OR lower(query) LIKE '%%update%%'
        OR lower(query) LIKE '%%merge%%'
    )
"""

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
                "type": "DatabaseLineage",
                "queryLogDuration": 1,
                "resultLimit": 1000,
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
workflow = LineageWorkflow.create(config)
workflow.execute()
workflow.raise_from_status()
