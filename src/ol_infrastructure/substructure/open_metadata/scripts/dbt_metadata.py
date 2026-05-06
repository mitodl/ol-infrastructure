"""dbt artifact metadata enrichment workflow for OpenMetadata.

Downloads manifest.json, catalog.json, and the most-recent run_results.json
from the Dagster S3 bucket (uploaded by DbtS3ArtifactsResource after each
full dbt build in the lakehouse code location) and enriches the existing
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
config — giving us both the manifest and the latest run_results.

IRSA provides ambient S3 and AWS credentials — no credential secret is needed.

Environment variables
---------------------
OM_SERVICE_NAME       OM database service to enrich (Trino / Starburst Galaxy).
OM_SERVER_URL         OpenMetadata API host:port.
OM_BOT_JWT_TOKEN      Ingestion-bot JWT (from om-ingestion-bot secret).
OM_AWS_REGION         AWS region for the S3 client.
OM_DBT_BUCKET         S3 bucket written by DbtS3ArtifactsResource.
OM_DBT_PREFIX         Key prefix (default: openmetadata/dbt-artifacts).
"""

import os
import tempfile
from pathlib import Path

import boto3
from metadata.workflow.metadata import MetadataWorkflow

_BUCKET = os.environ["OM_DBT_BUCKET"]
_PREFIX = os.environ.get("OM_DBT_PREFIX", "openmetadata/dbt-artifacts").rstrip("/")
_REGION = os.environ["OM_AWS_REGION"]

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


def _latest_run_results() -> str | None:
    """Return the S3 key of the most-recently modified run_results.json."""
    runs_prefix = f"{_PREFIX}/runs/"
    paginator = s3.get_paginator("list_objects_v2")
    best_key: str | None = None
    best_ts = None
    for page in paginator.paginate(Bucket=_BUCKET, Prefix=runs_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("run_results.json") and (
                best_ts is None or obj["LastModified"] > best_ts
            ):
                best_ts = obj["LastModified"]
                best_key = key
    return best_key


with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)

    # Fetch manifest (required)
    if not _download(f"{_PREFIX}/manifest.json", tmp / "manifest.json"):
        msg = f"manifest.json not found at s3://{_BUCKET}/{_PREFIX}/"
        raise FileNotFoundError(msg)

    # Fetch catalog (optional — dbt can run without it)
    _download(f"{_PREFIX}/catalog.json", tmp / "catalog.json")

    # Fetch the most recent run_results (optional — needed for test results)
    run_key = _latest_run_results()
    if run_key:
        _download(run_key, tmp / "run_results.json")

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
                    "dbtUpdateOwners": False,
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
