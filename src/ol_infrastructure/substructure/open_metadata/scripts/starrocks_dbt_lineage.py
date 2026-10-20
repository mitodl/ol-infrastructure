"""Data lake → StarRocks table lineage for OpenMetadata, from the dbt manifest.

The dbt models built on StarRocks read the lake's Iceberg tables through an
external catalog and declare them as dbt sources.  OpenMetadata holds those
tables under the Trino service and the models under the StarRocks service, so
an edge between them crosses services.

OM's dbt workflow can resolve a source in another service
(``searchAcrossDatabases``), but it also treats every source as a data model
of the table it finds.  Run against the StarRocks manifest it would replace
the description, tags and dbt model of each lake table with those of the
source declaration.  The dbt job for StarRocks therefore filters the lake
schemas out, and this script writes the edges on its own, touching nothing
else on either table.

OM's query-log lineage for StarRocks reads the FE audit log table
(``starrocks_audit_db__.starrocks_audit_tbl__``), which needs the AuditLoader
plugin.  The cluster does not run it.

Environment variables
---------------------
OM_SERVER_URL                OpenMetadata API base URL.
OM_BOT_JWT_TOKEN             JWT for the lineage bot (om-lineage-bot secret).
OM_STARROCKS_SERVICE_NAME    OM service holding the StarRocks models.
OM_STARROCKS_DATABASE_NAME   Service-level database name in that service.
OM_LAKE_SERVICE_NAME         OM service holding the lake tables (Trino).
OM_AWS_REGION                AWS region for the S3 client.
OM_DBT_BUCKET                S3 bucket written by DbtS3ArtifactsResource.
OM_DBT_PREFIX                Key prefix of the StarRocks dbt artifacts.
OM_DBT_SCHEMAS               JSON list of the StarRocks databases whose models
                             are linked.
"""

import json
import logging
import os
import sys
from typing import Any

import boto3
from metadata.generated.schema.api.lineage.addLineage import AddLineageRequest
from metadata.generated.schema.entity.data.table import Table
from metadata.generated.schema.entity.services.connections.metadata.openMetadataConnection import (  # noqa: E501
    AuthProvider,
    OpenMetadataConnection,
)
from metadata.generated.schema.security.client.openMetadataJWTClientConfig import (
    OpenMetadataJWTClientConfig,
)
from metadata.generated.schema.type.entityLineage import (
    EntitiesEdge,
    LineageDetails,
)
from metadata.generated.schema.type.entityLineage import Source as LineageSource
from metadata.generated.schema.type.entityReference import EntityReference
from metadata.ingestion.ometa.ometa_api import OpenMetadata
from metadata.utils import fqn

# Importing ``metadata`` already configured the root logger at WARNING, so
# without ``force`` this call is a no-op and every INFO line is dropped.
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    force=True,
)
log = logging.getLogger("starrocks-dbt-lineage")

_STARROCKS_SERVICE = os.environ["OM_STARROCKS_SERVICE_NAME"]
_STARROCKS_DATABASE = os.environ["OM_STARROCKS_DATABASE_NAME"]
_LAKE_SERVICE = os.environ["OM_LAKE_SERVICE_NAME"]
_BUCKET = os.environ["OM_DBT_BUCKET"]
_PREFIX = os.environ["OM_DBT_PREFIX"].rstrip("/")
_SCHEMAS = frozenset(json.loads(os.environ["OM_DBT_SCHEMAS"]))

metadata = OpenMetadata(
    config=OpenMetadataConnection(
        hostPort=os.environ["OM_SERVER_URL"],
        authProvider=AuthProvider.openmetadata,
        securityConfig=OpenMetadataJWTClientConfig(
            jwtToken=os.environ["OM_BOT_JWT_TOKEN"]
        ),
    )
)


def _table(service: str, database: str, schema: str, name: str) -> Table | None:
    """Return the OM table at the given coordinates, or None if OM has none."""
    return metadata.get_by_name(
        entity=Table,
        fqn=fqn.build(
            metadata,
            entity_type=Table,
            service_name=service,
            database_name=database,
            schema_name=schema,
            table_name=name,
            skip_es_search=True,
        ),
    )


def _entity_ref(table: Table) -> EntityReference:
    return EntityReference(id=table.id.root, type="table")


def _source_edges(manifest: dict[str, Any]) -> list[tuple[dict[str, Any], str, str]]:
    """Return ``(source node, model schema, model name)`` per source a model reads.

    :param manifest: Parsed dbt manifest.json.
    :returns: One entry for each source in the ``depends_on`` of a model that
        lives in one of the configured StarRocks databases.
    """
    return [
        (manifest["sources"][parent], node["schema"], node["alias"] or node["name"])
        for node in manifest["nodes"].values()
        if node["resource_type"] == "model" and node["schema"] in _SCHEMAS
        for parent in node["depends_on"]["nodes"]
        if parent in manifest["sources"]
    ]


manifest = json.loads(
    boto3.client("s3", region_name=os.environ["OM_AWS_REGION"])
    .get_object(Bucket=_BUCKET, Key=f"{_PREFIX}/manifest.json")["Body"]
    .read()
)

linked = 0
failed = 0
missing: set[str] = set()
for source, model_schema, model_name in _source_edges(manifest):
    source_table = source["identifier"] or source["name"]
    lake_table = _table(
        _LAKE_SERVICE, source["database"], source["schema"], source_table
    )
    if lake_table is None:
        missing.add(f"{source['database']}.{source['schema']}.{source_table}")
        continue
    starrocks_table = _table(
        _STARROCKS_SERVICE, _STARROCKS_DATABASE, model_schema, model_name
    )
    if starrocks_table is None:
        missing.add(f"{model_schema}.{model_name}")
        continue
    # add_lineage catches an API error, logs it and returns it under "error",
    # so a rejected write has to be read from the result.
    result = metadata.add_lineage(
        data=AddLineageRequest(
            edge=EntitiesEdge(
                fromEntity=_entity_ref(lake_table),
                toEntity=_entity_ref(starrocks_table),
                lineageDetails=LineageDetails(source=LineageSource.DbtLineage),
            )
        ),
        return_lineage=False,
    )
    if "error" in result:
        failed += 1
    else:
        linked += 1

# A table OM has not ingested is not this job's failure: the model may not have
# been built yet, or the metadata job that creates the table may not have run.
for name in sorted(missing):
    log.warning("No OpenMetadata table for %s; its edges were skipped", name)
log.info("Done. Linked=%d failed=%d tables-missing=%d", linked, failed, len(missing))

if failed:
    sys.exit(1)
