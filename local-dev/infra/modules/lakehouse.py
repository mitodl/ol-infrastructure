"""Local data lake for the local-dev infra stack: Gravitino and StarRocks.

Deployed only when `data-platform` is in enabled_apps. Gravitino serves the
Iceberg REST catalog over a RustFS bucket (objectstore.py) and keeps its state
on the shared CloudNativePG cluster. StarRocks is the query engine, with one
external catalog pointing at Gravitino.

This rehearses the deployed lake's shape (applications/gravitino,
applications/starrocks), not its security. Where local differs, and why:

- Gravitino runs the `simple` authenticator with authorization off, over plain
  HTTP. Deployed it validates Keycloak JWTs, enforces grants and serves HTTPS.
  A request with no Authorization header is `anonymous`, HTTP Basic sets the
  principal unchecked, and a Bearer header is refused with a 401.
- The catalog backend is JDBC on Postgres. Deployed it is the stock Iceberg
  GlueCatalog, which cannot store views, so a view-materialized dbt model
  builds here and fails in QA.
- Credential vending is `s3-secret-key`, which hands out the RustFS root key.
  Deployed it is `aws-irsa`.
- StarRocks is the allin1 image, shared-nothing, one FE and one BE in a pod.
  Deployed it is shared-data on an S3 storage volume, so internal tables and
  materialized views here never exercise that path.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

import pulumi_kubernetes as k8s
from pulumi import ResourceOptions

from bridge.lib.versions import (
    GRAVITINO_CHART_VERSION,
    GRAVITINO_SCHEMA_VERSION,
    GRAVITINO_VERSION,
    STARROCKS_VERSION,
)
from modules.objectstore import ACCESS_KEY, LAKE_BUCKET, SECRET_KEY

# enabled_apps key that switches this module on. It has no entry in the
# Tiltfile's APPS list: there is no app image to build, only infrastructure.
DATA_PLATFORM_APP = "data-platform"

NAMESPACE = "local-infra"
POSTGRES_HOST = f"local-pg-rw.{NAMESPACE}.svc.cluster.local"
S3_ENDPOINT = f"http://rustfs.{NAMESPACE}.svc.cluster.local:9000"
S3_REGION = "us-east-1"

GRAVITINO_IMAGE = f"apache/gravitino:{GRAVITINO_VERSION}"
GRAVITINO_MANAGEMENT_PORT = 8090
ICEBERG_REST_PORT = 9001
GRAVITINO_HOST = f"gravitino.{NAMESPACE}.svc.cluster.local"
GRAVITINO_MANAGEMENT_URL = f"http://{GRAVITINO_HOST}:{GRAVITINO_MANAGEMENT_PORT}"
ICEBERG_REST_URL = f"http://{GRAVITINO_HOST}:{ICEBERG_REST_PORT}/iceberg"

# Same metalake as the deployed stack, and the catalog follows its
# ol_data_lake_<env> naming, in both Gravitino and StarRocks.
METALAKE = "ol_data_platform"
CATALOG = "ol_data_lake_local"

# The schema the starrocks_local dbt profile in ol-data-platform connects to.
# dbt-starrocks creates a missing profile schema itself with a bare CREATE
# DATABASE from every thread, and the threads that lose that race fail the
# run, so it has to exist before the first dbt run.
WAREHOUSE_SCHEMA = "ol_warehouse_local"

# The StarRocks-native databases the b2b materialized views are built in, for
# the starrocks_local_b2b profile (Dagster's `dev`). b2b_analytics is that
# profile's schema, so it has the same race: the losing threads fail with
# "Can't create database 'b2b_analytics'; database exists". b2b_learner_records
# is here because the deployed clusters get both from substructure/starrocks.
NATIVE_DATABASES = ("b2b_analytics", "b2b_learner_records")

# Gravitino's entity store and the Iceberg JDBC catalog get a database each so
# dropping the lake's table metadata does not take the metalake with it.
GRAVITINO_DATABASE = "gravitino"
ICEBERG_DATABASE = "iceberg"

# The allin1 image tracks the version the deployed clusters run. Version
# parity is the reason local uses StarRocks at all, so do not float this.
STARROCKS_IMAGE = f"starrocks/allin1-ubuntu:{STARROCKS_VERSION}"
STARROCKS_HOME = "/data/deploy/starrocks"
STARROCKS_HOST = f"starrocks.{NAMESPACE}.svc.cluster.local"
STARROCKS_QUERY_PORT = 9030
STARROCKS_HTTP_PORT = 8030

_SCHEMA_FILE = f"schema-{GRAVITINO_SCHEMA_VERSION}-postgresql.sql"

# Gravitino only creates its own schema on H2, and the upstream file cannot be
# rerun (bare CREATE INDEX statements), hence the to_regclass guard. The app
# role has CREATEDB, which is what lets this run without a superuser.
_DATABASE_SCRIPT = f"""#!/bin/sh
set -eu

for database in {GRAVITINO_DATABASE} {ICEBERG_DATABASE}; do
    if [ -z "$(psql -tAc "SELECT 1 FROM pg_database WHERE datname = '${{database}}'")" ]; then
        echo "==> creating database: ${{database}}"
        createdb "${{database}}"
    fi
done

if [ -n "$(psql -d {GRAVITINO_DATABASE} -tAc "SELECT to_regclass('public.metalake_meta')")" ]; then
    echo "==> Gravitino schema already present"
    exit 0
fi
echo "==> applying {_SCHEMA_FILE}"
psql -d {GRAVITINO_DATABASE} -1 -v ON_ERROR_STOP=1 -f "/schema/{_SCHEMA_FILE}"
"""  # noqa: E501, S608

# Idempotent. The Job that runs it expires after ten minutes, so a reconcile
# later than that replays it, which is what restores the StarRocks catalog if
# its volume is ever deleted. IF NOT EXISTS and the 200 checks mean a changed
# property does not reach an existing catalog: drop the catalog first.
_CATALOG_SCRIPT = f"""#!/bin/sh
set -eu

metalakes="{GRAVITINO_MANAGEMENT_URL}/api/metalakes"

gravitino() {{
    curl -sS -o /dev/null -w '%{{http_code}}' \\
        -H 'Accept: application/vnd.gravitino.v1+json' \\
        -H 'Content-Type: application/json' "$@"
}}

starrocks() {{
    mysql -h {STARROCKS_HOST} -P {STARROCKS_QUERY_PORT} -u root "$@"
}}

echo "==> waiting for Gravitino"
until [ "$(gravitino "${{metalakes}}" || true)" = 200 ]; do
    sleep 3
done

if [ "$(gravitino "${{metalakes}}/{METALAKE}")" != 200 ]; then
    echo "==> creating metalake: {METALAKE}"
    gravitino -f -X POST "${{metalakes}}" -d @/bootstrap/metalake.json >/dev/null
fi
if [ "$(gravitino "${{metalakes}}/{METALAKE}/catalogs/{CATALOG}")" != 200 ]; then
    echo "==> creating Gravitino catalog: {CATALOG}"
    gravitino -f -X POST "${{metalakes}}/{METALAKE}/catalogs" -d @/bootstrap/catalog.json >/dev/null
fi

echo "==> waiting for StarRocks"
until starrocks -e 'SELECT 1' >/dev/null 2>&1; do
    sleep 3
done

echo "==> creating StarRocks catalog, schema and databases: {CATALOG}.{WAREHOUSE_SCHEMA}, {", ".join(NATIVE_DATABASES)}"
starrocks < /bootstrap/catalog.sql
echo "==> lakehouse bootstrap complete"
"""  # noqa: E501


def _gravitino_catalog() -> dict[str, object]:
    return {
        "name": CATALOG,
        "type": "RELATIONAL",
        "provider": "lakehouse-iceberg",
        "comment": "Local-dev data lake",
        "properties": {
            "catalog-backend": "jdbc",
            "uri": f"jdbc:postgresql://{POSTGRES_HOST}:5432/{ICEBERG_DATABASE}",
            "jdbc-driver": "org.postgresql.Driver",
            "jdbc-user": "app",
            "jdbc-password": "localdev",  # pragma: allowlist secret
            "jdbc-initialize": "true",
            "warehouse": f"s3://{LAKE_BUCKET}/warehouse",
            "io-impl": "org.apache.iceberg.aws.s3.S3FileIO",
            # Returned to every client in /v1/config and in each loadTable
            # response, where it overrides the client's own endpoint. A writer
            # outside the cluster cannot resolve it, so pyiceberg and dlt have
            # to run in a pod.
            "s3-endpoint": S3_ENDPOINT,
            "s3-access-key-id": ACCESS_KEY,
            "s3-secret-access-key": SECRET_KEY,
            "s3-region": S3_REGION,
            "s3-path-style-access": "true",
            "credential-providers": "s3-secret-key",
        },
    }


def _starrocks_catalog_sql() -> str:
    properties = {
        "type": "iceberg",
        "iceberg.catalog.type": "rest",
        "iceberg.catalog.uri": ICEBERG_REST_URL,
        # Without an explicit endpoint the BE sends its requests to AWS.
        "aws.s3.endpoint": S3_ENDPOINT,
        # Static keys, not vended credentials: on vended credentials alone
        # SELECT works and INSERT fails with "Access Denied".
        "aws.s3.access_key": ACCESS_KEY,
        "aws.s3.secret_key": SECRET_KEY,
        "aws.s3.region": S3_REGION,
        "aws.s3.enable_path_style_access": "true",
        "aws.s3.enable_ssl": "false",
    }
    rendered = ",\n".join(
        f"    {json.dumps(key)} = {json.dumps(value)}"
        for key, value in properties.items()
    )
    return (
        f"CREATE EXTERNAL CATALOG IF NOT EXISTS {CATALOG}\n"
        f"PROPERTIES (\n{rendered}\n);\n"
        f"CREATE DATABASE IF NOT EXISTS {CATALOG}.{WAREHOUSE_SCHEMA};\n"
    ) + "".join(
        f"CREATE DATABASE IF NOT EXISTS default_catalog.{database};\n"
        for database in NATIVE_DATABASES
    )


@dataclass
class LakehouseResources:
    """Resources other modules may need to depend on."""

    gravitino: k8s.helm.v3.Release
    starrocks: k8s.apps.v1.StatefulSet
    bootstrap_job: k8s.batch.v1.Job


def create_lakehouse(
    _k8s: Callable[..., ResourceOptions],
    local_infra_ns: k8s.core.v1.Namespace,
    db_cluster: k8s.apiextensions.CustomResource,
    object_store_bootstrap: k8s.batch.v1.Job,
    postgres_version: str,
) -> LakehouseResources:
    """Deploy Gravitino, StarRocks and the Job that joins them into a lake.

    In-cluster addresses: the Iceberg REST catalog is ICEBERG_REST_URL and
    StarRocks speaks the MySQL protocol at STARROCKS_HOST:9030 as `root` with
    no password.
    """
    bootstrap_files = {
        "databases.sh": _DATABASE_SCRIPT,
        "catalogs.sh": _CATALOG_SCRIPT,
        "metalake.json": json.dumps(
            {"name": METALAKE, "comment": "Local-dev metalake", "properties": {}}
        ),
        "catalog.json": json.dumps(_gravitino_catalog()),
        "catalog.sql": _starrocks_catalog_sql(),
    }
    # On the Jobs' pod templates, so an edit to any of these files replaces
    # the Jobs and runs them again instead of only updating the ConfigMap.
    bootstrap_checksum = hashlib.sha256(
        json.dumps(bootstrap_files, sort_keys=True).encode()
    ).hexdigest()
    scripts = k8s.core.v1.ConfigMap(
        "lakehouse-bootstrap",
        metadata={"name": "lakehouse-bootstrap", "namespace": NAMESPACE},
        data=bootstrap_files,
        opts=_k8s(parent=local_infra_ns),
    )

    database_job = k8s.batch.v1.Job(
        "lakehouse-databases",
        metadata={"name": "lakehouse-databases", "namespace": NAMESPACE},
        spec={
            "backoffLimit": 6,
            "ttlSecondsAfterFinished": 600,
            "template": {
                "metadata": {
                    "labels": {"app": "lakehouse-databases"},
                    "annotations": {"checksum/bootstrap": bootstrap_checksum},
                },
                "spec": {
                    "restartPolicy": "OnFailure",
                    "initContainers": [
                        {
                            "name": "copy-schema",
                            "image": GRAVITINO_IMAGE,
                            "command": [
                                "cp",
                                f"/opt/gravitino/scripts/postgresql/{_SCHEMA_FILE}",
                                "/schema/",
                            ],
                            "volumeMounts": [
                                {"name": "schema", "mountPath": "/schema"},
                            ],
                        }
                    ],
                    "containers": [
                        {
                            "name": "databases",
                            "image": (
                                f"ghcr.io/cloudnative-pg/postgresql:{postgres_version}"
                            ),
                            "command": ["/bin/sh", "/bootstrap/databases.sh"],
                            "env": [
                                {"name": "PGHOST", "value": POSTGRES_HOST},
                                {"name": "PGDATABASE", "value": "app"},
                                {
                                    "name": "PGUSER",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "pg-app-credentials",
                                            "key": "username",
                                        }
                                    },
                                },
                                {
                                    "name": "PGPASSWORD",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "pg-app-credentials",
                                            "key": "password",
                                        }
                                    },
                                },
                            ],
                            "volumeMounts": [
                                {"name": "schema", "mountPath": "/schema"},
                                {"name": "bootstrap", "mountPath": "/bootstrap"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "schema", "emptyDir": {}},
                        {
                            "name": "bootstrap",
                            "configMap": {"name": "lakehouse-bootstrap"},
                        },
                    ],
                },
            },
        },
        # A Job's spec is immutable; see the same option in objectstore.py.
        opts=_k8s(
            parent=local_infra_ns,
            depends_on=[db_cluster, scripts],
            delete_before_replace=True,
        ),
    )

    gravitino = k8s.helm.v3.Release(
        "gravitino",
        k8s.helm.v3.ReleaseArgs(
            name="gravitino",
            chart="oci://registry-1.docker.io/apache/gravitino-helm",
            version=GRAVITINO_CHART_VERSION,
            namespace=NAMESPACE,
            cleanup_on_fail=True,
            timeout=600,
            values={
                "fullnameOverride": "gravitino",
                "image": {"tag": GRAVITINO_VERSION},
                "entity": {
                    "jdbcUrl": (
                        f"jdbc:postgresql://{POSTGRES_HOST}:5432/"
                        f"{GRAVITINO_DATABASE}?currentSchema=public"
                    ),
                    "jdbcDriver": "org.postgresql.Driver",
                    "jdbcUser": "app",
                    "jdbcPassword": "localdev",  # pragma: allowlist secret
                },
                "authenticators": "simple",
                "auxService": {"names": "iceberg-rest"},
                # Same mode as the deployed stack: the backend and storage
                # settings live on the Gravitino catalog the bootstrap Job
                # creates, not in gravitino.conf. Until that catalog exists
                # the REST endpoint answers 404.
                "icebergRest": {
                    "catalogConfigProvider": "dynamic-config-provider",
                    "dynamicConfigProvider": {
                        "metalake": METALAKE,
                        "defaultCatalogName": CATALOG,
                    },
                },
                # Measured at 570Mi after a small workload with the chart's
                # default 1G heap.
                "resources": {
                    "requests": {"cpu": "100m", "memory": "768Mi"},
                    "limits": {"memory": "2Gi"},
                },
            },
        ),
        opts=_k8s(parent=local_infra_ns, depends_on=[database_job]),
    )

    starrocks = k8s.apps.v1.StatefulSet(
        "starrocks",
        metadata={"name": "starrocks", "namespace": NAMESPACE},
        spec={
            "replicas": 1,
            "selector": {"matchLabels": {"app": "starrocks"}},
            "serviceName": "starrocks",
            "template": {
                "metadata": {"labels": {"app": "starrocks"}},
                "spec": {
                    "containers": [
                        {
                            "name": "starrocks",
                            "image": STARROCKS_IMAGE,
                            "ports": [
                                {
                                    "containerPort": STARROCKS_QUERY_PORT,
                                    "name": "mysql",
                                },
                                {
                                    "containerPort": STARROCKS_HTTP_PORT,
                                    "name": "http",
                                },
                            ],
                            # FE metadata holds the external catalog and every
                            # internal table definition, BE storage holds
                            # their rows. Losing one without the other leaves
                            # tablets the FE cannot find, so they share a
                            # volume. The conf directories stay on the image:
                            # the entrypoint appends to fe.conf and be.conf.
                            "volumeMounts": [
                                {
                                    "name": "data",
                                    "mountPath": f"{STARROCKS_HOME}/fe/meta",
                                    "subPath": "fe-meta",
                                },
                                {
                                    "name": "data",
                                    "mountPath": f"{STARROCKS_HOME}/be/storage",
                                    "subPath": "be-storage",
                                },
                            ],
                            "readinessProbe": {
                                "tcpSocket": {"port": STARROCKS_QUERY_PORT},
                                "initialDelaySeconds": 15,
                                "periodSeconds": 5,
                            },
                            # The BE sizes itself from the cgroup limit (about
                            # 80% of it), not from the node. Idle is ~800Mi.
                            "resources": {
                                "requests": {"cpu": "500m", "memory": "1Gi"},
                                "limits": {"memory": "4Gi"},
                            },
                        }
                    ],
                },
            },
            "volumeClaimTemplates": [
                {
                    "metadata": {"name": "data"},
                    "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        "resources": {"requests": {"storage": "20Gi"}},
                    },
                }
            ],
        },
        opts=_k8s(parent=local_infra_ns),
    )

    starrocks_service = k8s.core.v1.Service(
        "starrocks-svc",
        metadata={"name": "starrocks", "namespace": NAMESPACE},
        spec={
            "selector": {"app": "starrocks"},
            "ports": [
                {
                    "name": "mysql",
                    "port": STARROCKS_QUERY_PORT,
                    "targetPort": STARROCKS_QUERY_PORT,
                },
                {
                    "name": "http",
                    "port": STARROCKS_HTTP_PORT,
                    "targetPort": STARROCKS_HTTP_PORT,
                },
            ],
        },
        opts=_k8s(parent=starrocks),
    )

    bootstrap_job = k8s.batch.v1.Job(
        "lakehouse-catalogs",
        metadata={"name": "lakehouse-catalogs", "namespace": NAMESPACE},
        spec={
            "backoffLimit": 6,
            # The script's wait loops never exit by themselves, so backoffLimit
            # cannot end a run where Gravitino or StarRocks never comes up.
            "activeDeadlineSeconds": 600,
            "ttlSecondsAfterFinished": 600,
            "template": {
                "metadata": {
                    "labels": {"app": "lakehouse-catalogs"},
                    "annotations": {"checksum/bootstrap": bootstrap_checksum},
                },
                "spec": {
                    "restartPolicy": "OnFailure",
                    # The StarRocks image is the one image here with both curl
                    # and a MySQL client. It is 3GB, so run where it is
                    # already pulled.
                    "affinity": {
                        "podAffinity": {
                            "requiredDuringSchedulingIgnoredDuringExecution": [
                                {
                                    "labelSelector": {
                                        "matchLabels": {"app": "starrocks"}
                                    },
                                    "topologyKey": "kubernetes.io/hostname",
                                }
                            ]
                        }
                    },
                    "containers": [
                        {
                            "name": "catalogs",
                            "image": STARROCKS_IMAGE,
                            "command": ["/bin/sh", "/bootstrap/catalogs.sh"],
                            "volumeMounts": [
                                {"name": "bootstrap", "mountPath": "/bootstrap"},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "bootstrap",
                            "configMap": {"name": "lakehouse-bootstrap"},
                        }
                    ],
                },
            },
        },
        opts=_k8s(
            parent=local_infra_ns,
            depends_on=[
                gravitino,
                starrocks,
                starrocks_service,
                object_store_bootstrap,
                scripts,
            ],
            delete_before_replace=True,
        ),
    )

    return LakehouseResources(
        gravitino=gravitino,
        starrocks=starrocks,
        bootstrap_job=bootstrap_job,
    )
