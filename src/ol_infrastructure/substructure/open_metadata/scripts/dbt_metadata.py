"""dbt artifact metadata enrichment workflow for OpenMetadata.

Downloads manifest.json, catalog.json, and the recent run_results.json files
from the Dagster S3 bucket (uploaded by DbtS3ArtifactsResource from the
lakehouse code location) and enriches the existing
Trino service tables in OpenMetadata with:
  - Model and column descriptions from dbt YAML docs
  - dbt model tags (stored under the "dbtTags" classification)
  - Test results (dbt test outcomes surfaced as OM test cases)
  - dbt lineage (model-to-model dependencies and source → model edges)

S3 layout produced by DbtS3ArtifactsResource
---------------------------------------------
  <prefix>/manifest.json          ← latest full build manifest (written once)
  <prefix>/catalog.json           ← latest catalog
  <prefix>/runs/<uuid>/run_results.json   ← per-run test/timing results

OM's built-in S3 connector groups artifacts by directory, so it cannot pair
the root-level manifest with the per-run run_results files.  Instead we use
boto3 to fetch the files ourselves, write them to /tmp, and feed OM a local
config.

Most Dagster runs build a subset of the project, so one run_results.json
covers only the models selected by that run.
run_results file, so the files from the lookback window are merged into one
that holds each node's most recent result.

IRSA provides ambient S3 and AWS credentials — no credential secret is needed.

Environment variables
---------------------
OM_SERVICE_NAME       OM database service to enrich (Trino / Starburst Galaxy).
OM_SERVER_URL         OpenMetadata API host:port.
OM_BOT_JWT_TOKEN      Ingestion-bot JWT (from om-ingestion-bot secret).
OM_AWS_REGION         AWS region for the S3 client.
OM_DBT_BUCKET         S3 bucket written by DbtS3ArtifactsResource.
OM_DBT_PREFIX         Key prefix (default: openmetadata/dbt-artifacts).
OM_DBT_RUN_RESULTS_LOOKBACK_HOURS
                      How far back to read run_results files (default: 48,
                      two schedule intervals, so one failed night loses nothing).
"""

import json
import os
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
from metadata.workflow.metadata import MetadataWorkflow

_BUCKET = os.environ["OM_DBT_BUCKET"]
_PREFIX = os.environ.get("OM_DBT_PREFIX", "openmetadata/dbt-artifacts").rstrip("/")
_REGION = os.environ["OM_AWS_REGION"]
_RUN_RESULTS_LOOKBACK_HOURS = int(
    os.environ.get("OM_DBT_RUN_RESULTS_LOOKBACK_HOURS", "48")
)

s3 = boto3.client("s3", region_name=_REGION)


def _download(key: str, dest: Path) -> bool:
    """Download *key* from _BUCKET to *dest*. Returns False if key missing."""
    try:
        s3.download_file(_BUCKET, key, str(dest))
    except s3.exceptions.ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise
    else:
        return True


def _recent_run_results(since: datetime) -> list[str]:
    """Return the S3 keys of run_results.json files modified after *since*.

    :param since: Lower bound on the object's LastModified.
    :returns: Keys ordered oldest to newest.
    """
    runs_prefix = f"{_PREFIX}/runs/"
    paginator = s3.get_paginator("list_objects_v2")
    recent = [
        (obj["LastModified"], obj["Key"])
        for page in paginator.paginate(Bucket=_BUCKET, Prefix=runs_prefix)
        for obj in page.get("Contents", [])
        if obj["Key"].endswith("run_results.json") and obj["LastModified"] > since
    ]
    return [key for _, key in sorted(recent)]


def _finished_at(result: dict[str, Any]) -> datetime:
    """Return when dbt last worked on a result's node.

    A node that fails while compiling has a compile timing and no execute
    timing, so every timing counts.  Skipped nodes carry none and sort first.
    """
    return max(
        (
            datetime.fromisoformat(timing["completed_at"])
            for timing in result["timing"]
            if timing.get("completed_at")
        ),
        default=datetime.min.replace(tzinfo=UTC),
    )


def _merge_run_results(run_results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Merge run_results documents, keeping each node's most recent result.

    A later run that skipped a node does not replace the outcome of a run
    that worked on it.

    :param run_results: Parsed run_results.json documents, oldest to newest.
        Must yield at least one.
    :returns: One run_results document carrying the newest run's metadata.
    """
    latest: dict[str, dict[str, Any]] = {}
    newest: dict[str, Any] = {}
    for document in run_results:
        for result in document["results"]:
            current = latest.get(result["unique_id"])
            if current is None or _finished_at(result) >= _finished_at(current):
                latest[result["unique_id"]] = result
        newest = document
    return {**newest, "results": list(latest.values())}


with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)

    # Fetch manifest (required)
    if not _download(f"{_PREFIX}/manifest.json", tmp / "manifest.json"):
        msg = f"manifest.json not found at s3://{_BUCKET}/{_PREFIX}/"
        raise FileNotFoundError(msg)

    # Fetch catalog (optional — dbt can run without it)
    _download(f"{_PREFIX}/catalog.json", tmp / "catalog.json")

    # Fetch the recent run_results (optional — needed for test results)
    run_keys = _recent_run_results(
        datetime.now(tz=UTC) - timedelta(hours=_RUN_RESULTS_LOOKBACK_HOURS)
    )
    if run_keys:
        # A generator, so the runs' documents are not all held in memory at once.
        documents = (
            json.loads(s3.get_object(Bucket=_BUCKET, Key=key)["Body"].read())
            for key in run_keys
        )
        (tmp / "run_results.json").write_text(json.dumps(_merge_run_results(documents)))

    config = {
        "source": {
            "type": "dbt",
            # Trino (Starburst Galaxy) is the query engine that owns the Iceberg
            # tables dbt writes to. Glue also catalogs the same tables, but OM
            # must match the service name the tables were ingested under.
            "serviceName": os.environ["OM_SERVICE_NAME"],
            "sourceConfig": {
                "config": {
                    "type": "DBT",
                    "dbtConfigSource": {
                        "dbtConfigType": "local",
                        "dbtCatalogFilePath": str(tmp / "catalog.json")
                        if (tmp / "catalog.json").exists()
                        else None,
                        "dbtManifestFilePath": str(tmp / "manifest.json"),
                        "dbtRunResultsFilePath": str(tmp / "run_results.json")
                        if (tmp / "run_results.json").exists()
                        else None,
                    },
                    "dbtUpdateDescriptions": True,
                    # A table with no owner takes the one its dbt node declares
                    # (meta.openmetadata.owner) whatever this is set to.  True
                    # also replaces an existing owner on a model's table, so
                    # one set in the UI lasts until the next run.  A source's
                    # table is never overwritten.
                    "dbtUpdateOwners": True,
                    "includeTags": True,
                    # Restrict to production dbt layer schemas to avoid attempting
                    # to resolve dev-namespace model names that don't exist in OM.
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
