"""Glue ↔ Trino table lineage for OpenMetadata.

Glue and Trino (Starburst Galaxy) both catalog the same underlying Iceberg
tables.  OM ingests them as separate database services, so by default they
appear as unrelated entities.  This script creates bidirectional lineage edges
between every (schema_name, table_name) pair that exists in both services,
making it clear in the OM UI that they are the same physical data.

Matching strategy
-----------------
FQNs differ by service prefix and may differ in catalog/database name:
  Glue FQN   → Glue.<database>.<schema>.<table>
  Trino FQN  → <service>.<catalog>.<schema>.<table>

We match only on (schema_name, table_name) to avoid hard-coding the catalog
or database name.  Only schemas that look like data-lake production schemas are
considered, which avoids false matches on identically-named utility tables in
unrelated catalogs.

Environment variables
---------------------
OM_SERVER_URL          OpenMetadata API base URL.
OM_BOT_JWT_TOKEN       JWT for the lineage bot (om-lineage-bot secret).
OM_GLUE_SERVICE_NAME   OM service name for the Glue catalog (default: Glue).
OM_TRINO_SERVICE_NAME  OM service name for Trino/Starburst (default: Starburst Galaxy).
"""

from __future__ import annotations

import logging
import os
import re
import sys
from http import HTTPStatus

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
from metadata.ingestion.ometa.client import APIError
from metadata.ingestion.ometa.ometa_api import OpenMetadata
from metadata.utils import fqn

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
log = logging.getLogger("glue-trino-lineage")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_SERVER_URL = os.environ["OM_SERVER_URL"]
_JWT_TOKEN = os.environ["OM_BOT_JWT_TOKEN"]
_GLUE_SERVICE = os.environ.get("OM_GLUE_SERVICE_NAME", "Glue")
_TRINO_SERVICE = os.environ.get("OM_TRINO_SERVICE_NAME", "Starburst Galaxy")

# Only link production data-lake schemas to avoid accidental cross-env matches.
_SCHEMA_RE = re.compile(r"^ol_(warehouse|data_lake)_[a-z]+_?")

# ---------------------------------------------------------------------------
# OM client
# ---------------------------------------------------------------------------
_server_config = OpenMetadataConnection(
    hostPort=_SERVER_URL,
    authProvider=AuthProvider.openmetadata,
    securityConfig=OpenMetadataJWTClientConfig(jwtToken=_JWT_TOKEN),
)
metadata = OpenMetadata(config=_server_config)


def _build_indexes(
    service_names: list[str],
) -> dict[str, dict[tuple[str, str], Table]]:
    """Return {service: {(schema_name, table_name): Table}} for *service_names*.

    The tables list endpoint has no service filter (it accepts ``database`` and
    ``databaseSchema`` and silently ignores anything else), so list every table
    once and split by the service component of the FQN.
    """
    indexes: dict[str, dict[tuple[str, str], Table]] = {
        name: {} for name in service_names
    }
    tables = metadata.list_all_entities(
        entity=Table,
        fields=["id", "name", "fullyQualifiedName", "databaseSchema"],
        limit=500,
    )
    for table in tables:
        if not table.fullyQualifiedName:
            continue
        parts = fqn.split(table.fullyQualifiedName.root)
        # FQN format: <service>.<catalog/database>.<schema>.<table>
        if len(parts) != 4 or parts[0] not in indexes:  # noqa: PLR2004
            continue
        service_name, _, schema_name, table_name = parts
        if not _SCHEMA_RE.match(schema_name):
            continue
        indexes[service_name][(schema_name, table_name)] = table
    for name, index in indexes.items():
        log.info("Indexed %d tables for service '%s'", len(index), name)
    return indexes


def _entity_ref(table: Table) -> EntityReference:
    return EntityReference(
        id=table.id.root,
        type="table",
        fullyQualifiedName=table.fullyQualifiedName.root
        if table.fullyQualifiedName
        else None,
    )


def _add_edge(from_table: Table, to_table: Table) -> None:
    if from_table.id.root == to_table.id.root:
        msg = f"Refusing to write a self-edge on {from_table.fullyQualifiedName}"
        raise ValueError(msg)
    req = AddLineageRequest(
        edge=EntitiesEdge(
            fromEntity=_entity_ref(from_table),
            toEntity=_entity_ref(to_table),
            lineageDetails=LineageDetails(source=LineageSource.ExternalTableLineage),
        )
    )
    try:
        metadata.add_lineage(data=req)
    except Exception:
        log.exception(
            "Failed to add lineage %s → %s",
            from_table.fullyQualifiedName,
            to_table.fullyQualifiedName,
        )
        raise


def _remove_self_edge(table: Table) -> None:
    """Delete the X → X edge that runs before the indexing fix wrote.

    Calls the endpoint directly rather than ``delete_lineage_edge``, which logs
    the 404 for an absent edge at ERROR. Absent is the normal case once the
    stale edges are gone.
    """
    table_id = table.id.root
    try:
        metadata.client.delete(f"/lineage/table/{table_id}/table/{table_id}")
    except APIError as err:
        if err.status_code != HTTPStatus.NOT_FOUND:
            raise
    else:
        log.info("Removed self-edge on %s", table.fullyQualifiedName)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
_indexes = _build_indexes([_GLUE_SERVICE, _TRINO_SERVICE])
glue_index = _indexes[_GLUE_SERVICE]
trino_index = _indexes[_TRINO_SERVICE]

common_keys = set(glue_index) & set(trino_index)
glue_only = set(glue_index) - set(trino_index)
trino_only = set(trino_index) - set(glue_index)

log.info(
    "Matched %d table pairs; %d Glue-only; %d Trino-only",
    len(common_keys),
    len(glue_only),
    len(trino_only),
)

linked = 0
errors = 0
for key in sorted(common_keys):
    glue_table = glue_index[key]
    trino_table = trino_index[key]
    _remove_self_edge(glue_table)
    _remove_self_edge(trino_table)
    try:
        # Bidirectional: Glue → Trino and Trino → Glue
        _add_edge(glue_table, trino_table)
        _add_edge(trino_table, glue_table)
        linked += 1
    except Exception:
        log.exception("Failed to link %s", key)
        errors += 1

log.info("Done. Linked=%d errors=%d", linked, errors)

if errors:
    sys.exit(1)
