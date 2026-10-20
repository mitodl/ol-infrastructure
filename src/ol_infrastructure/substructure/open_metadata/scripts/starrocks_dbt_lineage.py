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

Writing an edge never removes one, so after linking, the script deletes the
lake edges it owns that the manifest no longer declares: a model that stopped
reading a lake table, or a model removed from the project whose table is
still in OM.  It owns an edge whose source is ``DbtLineage``, whose upstream
is in the lake service and whose downstream is a table in one of the
configured StarRocks databases.  Edges with any other source (query-log
lineage, manual edits) and dbt edges between StarRocks tables, which OM's dbt
workflow writes, are left alone.

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
OM_LINEAGE_PRUNE_DRY_RUN     JSON boolean.  ``true`` logs the stale edges
                             without deleting them.
OM_LINEAGE_PRUNE_MAX_FRACTION
                             Largest share of the current lake edges one run
                             may delete before it refuses to prune.
"""

import json
import logging
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import boto3
from metadata.generated.schema.api.lineage.addLineage import AddLineageRequest
from metadata.generated.schema.entity.data.databaseSchema import DatabaseSchema
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

log = logging.getLogger("starrocks-dbt-lineage")

# The lineage API returns plain JSON, so edges are matched on the enum's value.
DBT_LINEAGE = "DbtLineage"
# At or below this many deletions the size guard does not apply, so a model
# that drops a source or two is pruned even when the graph is small.
PRUNE_GUARD_MIN_DELETES = 5
HTTP_NOT_FOUND = 404

ModelKey = tuple[str, str]


@dataclass(frozen=True)
class LakeEdge:
    """A lake table → StarRocks table edge as the lineage API lists it."""

    from_type: str
    from_id: str
    from_fqn: str
    to_id: str
    to_fqn: str


def model_key(schema: str, name: str) -> ModelKey:
    """Key a StarRocks table by schema and name, ignoring case."""
    return schema.casefold(), name.casefold()


def source_edges(
    manifest: dict[str, Any], schemas: frozenset[str]
) -> list[tuple[dict[str, Any], str, str]]:
    """Return ``(source node, model schema, model name)`` per source a model reads.

    :param manifest: Parsed dbt manifest.json.
    :param schemas: StarRocks databases whose models are linked.
    :returns: One entry for each source in the ``depends_on`` of a model that
        lives in one of ``schemas``.
    """
    return [
        (manifest["sources"][parent], node["schema"], node["alias"] or node["name"])
        for node in manifest["nodes"].values()
        if node["resource_type"] == "model" and node["schema"] in schemas
        for parent in node["depends_on"]["nodes"]
        if parent in manifest["sources"]
    ]


def lake_sources_by_model(
    manifest: dict[str, Any],
    schemas: frozenset[str],
    lake_fqn: Callable[[str, str, str], str],
) -> dict[ModelKey, set[str]]:
    """Return the casefolded lake table FQNs each StarRocks model reads.

    :param manifest: Parsed dbt manifest.json.
    :param schemas: StarRocks databases whose models are linked.
    :param lake_fqn: Builds a lake table FQN from database, schema and table.
    :returns: An entry for every model in ``schemas``, empty for a model that
        reads no source, so a model still in the project is told apart from one
        that was removed.
    """
    sources: dict[ModelKey, set[str]] = {
        model_key(node["schema"], node["alias"] or node["name"]): set()
        for node in manifest["nodes"].values()
        if node["resource_type"] == "model" and node["schema"] in schemas
    }
    for source, model_schema, model_name in source_edges(manifest, schemas):
        sources[model_key(model_schema, model_name)].add(
            lake_fqn(
                source["database"],
                source["schema"],
                source["identifier"] or source["name"],
            ).casefold()
        )
    return sources


def lake_dbt_edges(lineage: dict[str, Any], lake_prefix: str) -> list[LakeEdge]:
    """Return the edges this job owns among a table's upstream edges.

    :param lineage: ``GET /v1/lineage/table/{id}`` response for a StarRocks
        table, fetched with ``upstreamDepth=1``.
    :param lake_prefix: Casefolded FQN prefix of the lake service, with the
        trailing separator.
    :returns: Edges into the table whose source is ``DbtLineage`` and whose
        upstream node is in the lake service.
    """
    table = lineage["entity"]
    nodes = {node["id"]: node for node in lineage["nodes"]}
    edges = []
    for edge in lineage["upstreamEdges"]:
        # lineageDetails and its source are optional in the API schema.
        details = edge.get("lineageDetails") or {}
        upstream = nodes[edge["fromEntity"]]
        if (
            edge["toEntity"] == table["id"]
            and details.get("source") == DBT_LINEAGE
            and upstream["fullyQualifiedName"].casefold().startswith(lake_prefix)
        ):
            edges.append(
                LakeEdge(
                    from_type=upstream["type"],
                    from_id=upstream["id"],
                    from_fqn=upstream["fullyQualifiedName"],
                    to_id=table["id"],
                    to_fqn=table["fullyQualifiedName"],
                )
            )
    return edges


def stale_edges(edges: Iterable[LakeEdge], expected: set[str]) -> list[LakeEdge]:
    """Return the edges whose lake table the model no longer reads.

    :param edges: Lake edges into one StarRocks table.
    :param expected: Casefolded lake FQNs the manifest says the model reads;
        empty for a table that is no longer a model.
    """
    return [edge for edge in edges if edge.from_fqn.casefold() not in expected]


def prune_refusal(
    model_count: int, stale_count: int, current_count: int, max_fraction: float
) -> str | None:
    """Return why pruning should not run, or None when it may.

    A manifest that is empty, comes from the wrong target, or lists the wrong
    schemas looks like every model disappeared, and pruning against it would
    delete every lake edge.  The first check catches a manifest with no
    StarRocks models at all; the second a run that would delete a larger share
    of the edges than routine model changes do.

    :param model_count: StarRocks models in the manifest.
    :param stale_count: Edges the run would delete.
    :param current_count: Lake edges into StarRocks tables in OM now.
    :param max_fraction: Largest share of ``current_count`` a run may delete.
    """
    if model_count == 0:
        return "the manifest has no models in the configured StarRocks databases"
    if (
        stale_count > PRUNE_GUARD_MIN_DELETES
        and stale_count > max_fraction * current_count
    ):
        return (
            f"{stale_count} of {current_count} lake edges would be deleted, more "
            f"than the {max_fraction:.0%} limit (OM_LINEAGE_PRUNE_MAX_FRACTION)"
        )
    return None


@dataclass(frozen=True)
class Settings:
    """Job configuration read from the environment."""

    starrocks_service: str
    starrocks_database: str
    lake_service: str
    schemas: frozenset[str]
    dry_run: bool
    max_fraction: float


def _table_fqn(
    metadata: OpenMetadata, service: str, database: str, schema: str, name: str
) -> str:
    return fqn.build(
        metadata,
        entity_type=Table,
        service_name=service,
        database_name=database,
        schema_name=schema,
        table_name=name,
        skip_es_search=True,
    )


def _entity_ref(table: Table) -> EntityReference:
    return EntityReference(id=table.id.root, type="table")


def _link(
    metadata: OpenMetadata, manifest: dict[str, Any], settings: Settings
) -> tuple[int, int, set[str]]:
    """Write an edge for each lake table a StarRocks model reads.

    :returns: Edges written, edges OM rejected, and the tables OM has not
        ingested.
    """
    linked = 0
    failed = 0
    missing: set[str] = set()
    for source, model_schema, model_name in source_edges(manifest, settings.schemas):
        source_table = source["identifier"] or source["name"]
        lake_table = metadata.get_by_name(
            entity=Table,
            fqn=_table_fqn(
                metadata,
                settings.lake_service,
                source["database"],
                source["schema"],
                source_table,
            ),
        )
        if lake_table is None:
            missing.add(f"{source['database']}.{source['schema']}.{source_table}")
            continue
        starrocks_table = metadata.get_by_name(
            entity=Table,
            fqn=_table_fqn(
                metadata,
                settings.starrocks_service,
                settings.starrocks_database,
                model_schema,
                model_name,
            ),
        )
        if starrocks_table is None:
            missing.add(f"{model_schema}.{model_name}")
            continue
        # add_lineage catches an API error, logs it and returns it under
        # "error", so a rejected write has to be read from the result.
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
    return linked, failed, missing


@dataclass(frozen=True)
class PrunePlan:
    """The stale edges a run found, and why it may not delete them."""

    stale: list[LakeEdge]
    refusal: str | None
    failed: int


def _plan_prune(
    metadata: OpenMetadata, manifest: dict[str, Any], settings: Settings
) -> PrunePlan:
    """Find the lake edges into StarRocks that the manifest no longer declares.

    Every table in the configured StarRocks databases is checked, not only the
    manifest's models, so a removed model whose table OM still lists loses its
    edges too.  This runs before linking: the guard has to weigh the stale
    edges against the graph as it stood, because a bad manifest's own edges
    would otherwise inflate the total and let it delete every correct one.
    """
    expected = lake_sources_by_model(
        manifest,
        settings.schemas,
        lambda database, schema, table: _table_fqn(
            metadata, settings.lake_service, database, schema, table
        ),
    )
    lake_prefix = f"{fqn.quote_name(settings.lake_service)}.".casefold()
    current: list[LakeEdge] = []
    stale: list[LakeEdge] = []
    failed = 0
    for schema in sorted(settings.schemas):
        schema_fqn = fqn.build(
            metadata,
            entity_type=DatabaseSchema,
            service_name=settings.starrocks_service,
            database_name=settings.starrocks_database,
            schema_name=schema,
        )
        for table in metadata.list_all_entities(
            entity=Table, params={"databaseSchema": schema_fqn}
        ):
            # get_lineage_by_id logs an API error and returns None.
            lineage = metadata.get_lineage_by_id(
                entity=Table, entity_id=table.id.root, up_depth=1, down_depth=0
            )
            if lineage is None:
                failed += 1
                continue
            edges = lake_dbt_edges(lineage, lake_prefix)
            current.extend(edges)
            stale.extend(
                stale_edges(
                    edges, expected.get(model_key(schema, table.name.root), set())
                )
            )

    refusal = prune_refusal(
        len(expected), len(stale), len(current), settings.max_fraction
    )
    verb = "Would delete" if settings.dry_run or refusal else "Deleting"
    for edge in stale:
        log.info("%s stale lake edge %s -> %s", verb, edge.from_fqn, edge.to_fqn)
    if refusal:
        log.error("Not pruning stale lake edges: %s", refusal)
    return PrunePlan(stale=stale, refusal=refusal, failed=failed)


def _apply_prune(
    metadata: OpenMetadata, plan: PrunePlan, settings: Settings
) -> tuple[int, int]:
    """Delete the planned edges unless the plan was refused or this is a dry run.

    :returns: Edges deleted (or that would be, in a dry run) and failures.
    """
    if plan.refusal:
        return 0, plan.failed + 1
    if settings.dry_run:
        return len(plan.stale), plan.failed
    deleted, delete_failed = _delete_edges(metadata, plan.stale)
    return deleted, plan.failed + delete_failed


@dataclass(frozen=True)
class RunCounts:
    """What one run linked and pruned."""

    linked: int
    link_failed: int
    missing: set[str]
    pruned: int
    prune_failed: int


def _sync(
    metadata: OpenMetadata, manifest: dict[str, Any], settings: Settings
) -> RunCounts:
    """Plan the prune against the graph as it stands, link, then prune."""
    plan = _plan_prune(metadata, manifest, settings)
    linked, link_failed, missing = _link(metadata, manifest, settings)
    pruned, prune_failed = _apply_prune(metadata, plan, settings)
    return RunCounts(
        linked=linked,
        link_failed=link_failed,
        missing=missing,
        pruned=pruned,
        prune_failed=prune_failed,
    )


def _delete_edges(metadata: OpenMetadata, edges: list[LakeEdge]) -> tuple[int, int]:
    """Delete each edge, counting the deletions and the failures.

    ``delete_lineage_edge`` logs an API error and returns, so the client is
    called directly to count a rejected delete as a failure.
    """
    deleted = 0
    failed = 0
    for edge in edges:
        try:
            metadata.client.delete(
                f"/lineage/{edge.from_type}/{edge.from_id}/table/{edge.to_id}"
            )
        except APIError as err:
            if err.status_code == HTTP_NOT_FOUND:
                log.info("Edge %s -> %s was already gone", edge.from_fqn, edge.to_fqn)
                continue
            log.error(  # noqa: TRY400
                "Deleting %s -> %s failed: %s", edge.from_fqn, edge.to_fqn, err
            )
            failed += 1
        else:
            deleted += 1
    return deleted, failed


def main() -> None:
    """Link the lake tables to the StarRocks models, then prune stale links."""
    # Importing ``metadata`` already configured the root logger at WARNING, so
    # without ``force`` this call is a no-op and every INFO line is dropped.
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        force=True,
    )
    settings = Settings(
        starrocks_service=os.environ["OM_STARROCKS_SERVICE_NAME"],
        starrocks_database=os.environ["OM_STARROCKS_DATABASE_NAME"],
        lake_service=os.environ["OM_LAKE_SERVICE_NAME"],
        schemas=frozenset(json.loads(os.environ["OM_DBT_SCHEMAS"])),
        dry_run=json.loads(os.environ["OM_LINEAGE_PRUNE_DRY_RUN"]),
        max_fraction=float(os.environ["OM_LINEAGE_PRUNE_MAX_FRACTION"]),
    )
    metadata = OpenMetadata(
        config=OpenMetadataConnection(
            hostPort=os.environ["OM_SERVER_URL"],
            authProvider=AuthProvider.openmetadata,
            securityConfig=OpenMetadataJWTClientConfig(
                jwtToken=os.environ["OM_BOT_JWT_TOKEN"]
            ),
        )
    )
    prefix = os.environ["OM_DBT_PREFIX"].rstrip("/")
    manifest = json.loads(
        boto3.client("s3", region_name=os.environ["OM_AWS_REGION"])
        .get_object(Bucket=os.environ["OM_DBT_BUCKET"], Key=f"{prefix}/manifest.json")[
            "Body"
        ]
        .read()
    )

    counts = _sync(metadata, manifest, settings)
    # A table OM has not ingested is not this job's failure: the model may not
    # have been built yet, or the metadata job that creates the table may not
    # have run.
    for name in sorted(counts.missing):
        log.warning("No OpenMetadata table for %s; its edges were skipped", name)
    log.info(
        "Done. Linked=%d failed=%d tables-missing=%d pruned=%d%s prune-failed=%d",
        counts.linked,
        counts.link_failed,
        len(counts.missing),
        counts.pruned,
        " (dry run)" if settings.dry_run else "",
        counts.prune_failed,
    )

    if counts.link_failed or counts.prune_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
