"""Custom Dagster metadata ingestion for OpenMetadata.

Maps Dagster asset groups to OM Pipeline entities, replacing the manually-
created job that used an internal K8s service URL and grouped all auto-
materialized assets under the synthetic ``__ASSET_JOB`` name.

Improvements over the built-in OM Dagster connector:

- Source URLs point to the **external** Dagster UI (not the internal K8s
  service address), so links in OM are clickable by users.
- One OM Pipeline per *asset group* (per code location) instead of one per
  Dagster job, so dbt model groups, Airbyte sync groups, and other logical
  collections each appear as a named pipeline rather than all collapsing into
  the opaque ``__ASSET_JOB`` entity.
- OM Tasks within each pipeline are the asset nodes themselves (with their
  Dagster asset page URL as ``sourceUrl``), not the underlying op/solid
  handles that the built-in connector uses.
- Intra-group asset dependencies become OM ``downstreamTasks`` edges,
  preserving within-pipeline execution order.
- Inter-group asset dependencies become OM Pipeline-to-Pipeline lineage
  edges, giving a clear view of how data flows across groups and locations.
- Groups already covered by dedicated OM connectors (Superset datasets) are
  skipped to avoid duplicate entities.

Environment variables
---------------------
OM_SERVER_URL          OM REST API base URL (default: http://openmetadata:8585/api)
OM_BOT_JWT_TOKEN       Bot JWT for OM authentication (injected by _make_cronjob)
DAGSTER_INTERNAL_URL   Dagster webserver URL reachable from within the cluster
                       (default: http://dagster-dagster-webserver.dagster.svc.cluster.local:3000)
DAGSTER_EXTERNAL_URL   Externally reachable Dagster UI URL used for sourceUrl
                       values embedded in OM entities
DAGSTER_SERVICE_NAME   Name of the OM Pipeline service to create or update
                       (default: OL Orchestration)
"""

# NOTE: ingestion-base runs Python 3.10; enable PEP 604 union syntax.
from __future__ import annotations

import logging
import os
import re
import sys
import traceback
from collections import defaultdict
from typing import Any

import requests
from metadata.generated.schema.api.data.createPipeline import CreatePipelineRequest
from metadata.generated.schema.api.lineage.addLineage import AddLineageRequest
from metadata.generated.schema.api.services.createPipelineService import (
    CreatePipelineServiceRequest,
)
from metadata.generated.schema.entity.data.pipeline import Task
from metadata.generated.schema.entity.services.connections.metadata.openMetadataConnection import (  # noqa: E501
    AuthProvider,
    OpenMetadataConnection,
)
from metadata.generated.schema.entity.services.connections.pipeline.dagsterConnection import (  # noqa: E501
    DagsterConnection,
)
from metadata.generated.schema.entity.services.pipelineService import (
    PipelineConnection,
    PipelineService,
    PipelineServiceType,
)
from metadata.generated.schema.security.client.openMetadataJWTClientConfig import (
    OpenMetadataJWTClientConfig,
)
from metadata.generated.schema.type.basic import (
    EntityName,
    FullyQualifiedEntityName,
    SourceUrl,
)
from metadata.generated.schema.type.entityLineage import (
    EntitiesEdge,
    LineageDetails,
)
from metadata.generated.schema.type.entityLineage import Source as LineageSource
from metadata.generated.schema.type.entityReference import EntityReference
from metadata.ingestion.ometa.ometa_api import OpenMetadata

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, stream=sys.stdout)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OM_SERVER_URL = os.environ.get("OM_SERVER_URL", "http://openmetadata:8585/api")
OM_JWT_TOKEN = os.environ["OM_BOT_JWT_TOKEN"]
DAGSTER_INTERNAL_URL = os.environ.get(
    "DAGSTER_INTERNAL_URL",
    "http://dagster-dagster-webserver.dagster.svc.cluster.local:3000",
)
DAGSTER_EXTERNAL_URL = os.environ.get(
    "DAGSTER_EXTERNAL_URL", "https://pipelines.odl.mit.edu"
)
DAGSTER_SERVICE_NAME = os.environ.get("DAGSTER_SERVICE_NAME", "OL Orchestration")

# Asset groups already captured by dedicated OM connectors - skip them to
# avoid creating duplicate entities that compete with the Superset connector.
_SKIP_GROUPS: frozenset[str] = frozenset(
    {"superset_dataset", "superset_starrocks_dataset"}
)

# Synthetic Dagster job names that should not be surfaced as pipeline names.
_SYNTHETIC_JOB_RE = re.compile(r"^__")

# ---------------------------------------------------------------------------
# GraphQL queries
# ---------------------------------------------------------------------------

_REPOS_QUERY = """
{
  repositoriesOrError {
    __typename
    ... on RepositoryConnection {
      nodes {
        id
        name
        location { name }
      }
    }
  }
}
"""

_ASSETS_QUERY = """
query AssetsQuery($selector: RepositorySelector!) {
  repositoryOrError(repositorySelector: $selector) {
    __typename
    ... on Repository {
      assetNodes {
        id
        assetKey { path }
        groupName
        description
        computeKind
        jobs { name }
        dependencies {
          asset { assetKey { path } }
        }
      }
    }
  }
}
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute a GraphQL query against the internal Dagster endpoint."""
    resp = requests.post(
        f"{DAGSTER_INTERNAL_URL}/graphql",
        json={"query": query, "variables": variables or {}},
        timeout=120,
    )
    resp.raise_for_status()
    result = resp.json()
    if errors := result.get("errors"):
        msg = f"GraphQL errors: {errors}"
        raise RuntimeError(msg)
    return result


def _safe_name(text: str) -> str:
    """Return a string safe for use as an OM entity name (alphanumeric + _-.)."""
    return re.sub(r"[^a-zA-Z0-9_\-.]", "_", text)


def _task_name(path: list[str]) -> str:
    """Return a stable, unique task name from a full asset key path.

    Uses the full path joined with ``__`` rather than only the last segment to
    prevent within-group collisions when two assets share a common suffix (e.g.
    ``["bronze", "users"]`` and ``["silver", "users"]`` in the same group).
    """
    return _safe_name("__".join(path))


def _asset_source_url(path: list[str]) -> str:
    return f"{DAGSTER_EXTERNAL_URL}/assets/{'/'.join(path)}"


def _pipeline_name(location: str, group: str) -> str:
    return _safe_name(f"{location}__{group}")


def _explicit_job_url(location: str, group_assets: list[dict[str, Any]]) -> str | None:
    """Return the Dagster URL for the first explicit (non-synthetic) job.

    Returns ``None`` if all jobs covering this group's assets are synthetic.
    """
    jobs: set[str] = set()
    for asset in group_assets:
        for j in asset.get("jobs") or []:
            if not _SYNTHETIC_JOB_RE.match(j["name"]):
                jobs.add(j["name"])
    if jobs:
        job_name = sorted(jobs)[0]
        return f"{DAGSTER_EXTERNAL_URL}/locations/{location}/jobs/{job_name}"
    return None


def _get_or_create_service(metadata: OpenMetadata) -> PipelineService:
    """Retrieve the OM Pipeline service, creating it if absent."""
    service = metadata.get_by_name(entity=PipelineService, fqn=DAGSTER_SERVICE_NAME)
    if service:
        return service
    return metadata.create_or_update(
        CreatePipelineServiceRequest(
            name=EntityName(DAGSTER_SERVICE_NAME),
            serviceType=PipelineServiceType.Dagster,
            connection=PipelineConnection(
                config=DagsterConnection(host=DAGSTER_EXTERNAL_URL),
            ),
        )
    )


def _build_tasks(group_assets: list[dict[str, Any]]) -> list[Task]:
    """Build OM Task objects for all assets in a group.

    Uses the full asset key path as the task identity to prevent name
    collisions between assets that share a common last-segment name.
    Populates ``downstreamTasks`` for intra-group dependencies.
    """
    group_keys = {_task_name(a["assetKey"]["path"]) for a in group_assets}

    downstream_in_group: dict[str, list[str]] = defaultdict(list)
    for asset in group_assets:
        task_name = _task_name(asset["assetKey"]["path"])
        for dep in asset.get("dependencies") or []:
            dep_name = _task_name(dep["asset"]["assetKey"]["path"])
            if dep_name in group_keys:
                # dep_name is upstream of task_name; task_name is downstream.
                downstream_in_group[dep_name].append(task_name)

    tasks: list[Task] = []
    for asset in group_assets:
        task_name = _task_name(asset["assetKey"]["path"])
        downstream = downstream_in_group.get(task_name) or None
        tasks.append(
            Task(
                name=task_name,
                displayName="/".join(asset["assetKey"]["path"]),
                description=asset.get("description"),
                sourceUrl=SourceUrl(_asset_source_url(asset["assetKey"]["path"])),
                downstreamTasks=downstream,
            )
        )
    return tasks


def _ingest_repo(  # noqa: C901
    metadata: OpenMetadata,
    repo: dict[str, Any],
    asset_to_pipeline: dict[str, str],
    pipeline_ids: dict[str, str],
    cross_deps: list[tuple[str, str]],
) -> None:
    """Create OM Pipeline entities for every asset group in one Dagster repo."""
    location: str = repo["location"]["name"]
    repo_name: str = repo["name"]

    try:
        assets_result = _gql(
            _ASSETS_QUERY,
            {
                "selector": {
                    "repositoryName": repo_name,
                    "repositoryLocationName": location,
                }
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Failed to query assets for %s:\n%s", location, traceback.format_exc()
        )
        return

    repo_data = assets_result["data"]["repositoryOrError"]
    if repo_data.get("__typename") != "Repository":
        logger.warning(
            "Unexpected response type '%s' for location %s, skipping",
            repo_data.get("__typename"),
            location,
        )
        return

    asset_nodes: list[dict[str, Any]] = repo_data.get("assetNodes") or []
    if not asset_nodes:
        logger.info("Location %s: no assets, skipping", location)
        return

    # Group assets by groupName; normalise None/empty to "default".
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in asset_nodes:
        groups[node.get("groupName") or "default"].append(node)

    for group_name, group_assets in groups.items():
        if group_name in _SKIP_GROUPS:
            continue

        pname = _pipeline_name(location, group_name)

        # Register every asset in this group and collect all dependency pairs
        # (both intra- and inter-group) for the lineage phase.
        for asset in group_assets:
            akey = "/".join(asset["assetKey"]["path"])
            asset_to_pipeline[akey] = pname
            for dep in asset.get("dependencies") or []:
                dep_key = "/".join(dep["asset"]["assetKey"]["path"])
                cross_deps.append((dep_key, akey))

        job_url = _explicit_job_url(location, group_assets)
        pipeline_source_url = (
            job_url if job_url else f"{DAGSTER_EXTERNAL_URL}/asset-groups/{group_name}"
        )
        tasks = _build_tasks(group_assets)

        pipeline_req = CreatePipelineRequest(
            name=EntityName(pname),
            displayName=f"{location} / {group_name}",
            service=FullyQualifiedEntityName(DAGSTER_SERVICE_NAME),
            tasks=tasks or None,
            sourceUrl=SourceUrl(pipeline_source_url),
        )
        try:
            pipeline_entity = metadata.create_or_update(pipeline_req)
        except Exception:
            logger.exception("Error creating pipeline '%s'", pname)
            continue

        if pipeline_entity:
            pipeline_ids[pname] = str(pipeline_entity.id.root)
            logger.info("Pipeline '%s' -> %d tasks", pname, len(tasks))
        else:
            logger.warning("create_or_update returned None for pipeline '%s'", pname)


def _add_cross_lineage(
    metadata: OpenMetadata,
    pipeline_ids: dict[str, str],
    asset_to_pipeline: dict[str, str],
    cross_deps: list[tuple[str, str]],
) -> None:
    """Create OM Pipeline-to-Pipeline lineage edges from cross-group deps."""
    lineage_pairs: set[tuple[str, str]] = set()
    for upstream_key, downstream_key in cross_deps:
        from_pipeline = asset_to_pipeline.get(upstream_key)
        to_pipeline = asset_to_pipeline.get(downstream_key)
        if (
            from_pipeline
            and to_pipeline
            and from_pipeline != to_pipeline
            and from_pipeline in pipeline_ids
            and to_pipeline in pipeline_ids
        ):
            lineage_pairs.add((from_pipeline, to_pipeline))

    for from_p, to_p in sorted(lineage_pairs):
        try:
            metadata.add_lineage(
                AddLineageRequest(
                    edge=EntitiesEdge(
                        fromEntity=EntityReference(
                            id=pipeline_ids[from_p], type="pipeline"
                        ),
                        toEntity=EntityReference(
                            id=pipeline_ids[to_p], type="pipeline"
                        ),
                        lineageDetails=LineageDetails(
                            source=LineageSource.PipelineLineage
                        ),
                    )
                )
            )
            logger.info("Lineage: %s -> %s", from_p, to_p)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Lineage %s -> %s failed:\n%s",
                from_p,
                to_p,
                traceback.format_exc(),
            )


def main() -> None:
    """Run Dagster metadata ingestion: create Pipeline entities and lineage."""
    om_cfg = OpenMetadataConnection(
        hostPort=OM_SERVER_URL,
        authProvider=AuthProvider.openmetadata,
        securityConfig=OpenMetadataJWTClientConfig(jwtToken=OM_JWT_TOKEN),
    )
    metadata = OpenMetadata(om_cfg)
    _get_or_create_service(metadata)

    asset_to_pipeline: dict[str, str] = {}
    pipeline_ids: dict[str, str] = {}
    cross_deps: list[tuple[str, str]] = []

    repos_result = _gql(_REPOS_QUERY)
    repos_nodes = repos_result["data"]["repositoriesOrError"]["nodes"]

    for repo in repos_nodes:
        _ingest_repo(
            metadata,
            repo,
            asset_to_pipeline=asset_to_pipeline,
            pipeline_ids=pipeline_ids,
            cross_deps=cross_deps,
        )

    _add_cross_lineage(metadata, pipeline_ids, asset_to_pipeline, cross_deps)
    logger.info("Dagster metadata ingestion complete.")


if __name__ == "__main__":
    main()
