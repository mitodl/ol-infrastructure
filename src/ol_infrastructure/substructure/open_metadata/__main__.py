"""OpenMetadata Kubernetes ingestion jobs substructure.

Creates CronOMJob custom resources (via the omjob-operator CRD) for each
metadata connector, plus the IRSA role for the ``openmetadata-ingestion``
service account so ingestion pods can access AWS Glue, S3, and RDS IAM auth.

Connector credentials are read from Kubernetes secrets managed by the
OpenMetadata application stack (via Vault Secrets Operator). The ingestion-bot
JWT is injected the same way.

CronOMJob pods authenticate to the OpenMetadata server API using the bot JWT
token from the ``om-ingestion-bot`` K8s secret, and to data sources via
connector-specific K8s secrets (``om-connector-{name}``).
"""

import hashlib
from pathlib import Path

import pulumi_kubernetes as kubernetes
from pulumi import Config, Output, ResourceOptions, StackReference, export
from pulumi_aws import get_caller_identity, iam

from bridge.lib.versions import OPEN_METADATA_VERSION
from ol_infrastructure.components.aws.eks import OLEKSTrustRole, OLEKSTrustRoleConfig
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import setup_k8s_provider
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack, stack_ref

stack_info = parse_stack()
om_config = Config("open_metadata")

aws_region = om_config.get("aws_region") or "us-east-1"
aws_account = get_caller_identity()
aws_config = AWSBase(
    tags={"OU": "data", "Environment": f"data-{stack_info.env_suffix}"},
)

cluster_stack = StackReference(stack_ref(projects.EKS, f"data.{stack_info.name}"))
superset_stack = StackReference(stack_ref(projects.SUPERSET, stack_info.name))

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

open_metadata_namespace = "open-metadata"
ingestion_sa_name = "openmetadata-ingestion"

# ---------------------------------------------------------------------------
# IRSA role for the ingestion SA — Glue + S3 + RDS IAM auth
# ---------------------------------------------------------------------------

ingestion_iam_policy = iam.Policy(
    f"om-ingestion-iam-policy-{stack_info.env_suffix}",
    name=f"om-ingestion-policy-{stack_info.env_suffix}",
    path=f"/ol-substructure/open-metadata/{stack_info.env_suffix}/",
    description=(
        "Grants OpenMetadata ingestion pods read access to Glue catalog,"
        " S3 data lake, and RDS IAM auth for all app databases"
    ),
    policy=lint_iam_policy(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Sid": "GlueReadOnly",
                    "Effect": "Allow",
                    "Action": [
                        "glue:GetDatabase",
                        "glue:GetDatabases",
                        "glue:GetTable",
                        "glue:GetTables",
                        "glue:GetPartition",
                        "glue:GetPartitions",
                    ],
                    "Resource": [
                        "arn:aws:glue:*:*:catalog",
                        f"arn:aws:glue:*:*:database/*{stack_info.env_suffix}*",
                        f"arn:aws:glue:*:*:table/*{stack_info.env_suffix}*/*",
                    ],
                },
                {
                    "Sid": "S3DataLakeRead",
                    "Effect": "Allow",
                    "Action": [
                        "s3:GetObject",
                        "s3:ListBucket",
                    ],
                    "Resource": [
                        f"arn:aws:s3:::ol-data-lake-*-{stack_info.env_suffix}",
                        f"arn:aws:s3:::ol-data-lake-*-{stack_info.env_suffix}/*",
                    ],
                },
                {
                    # Allows reading dbt artifacts (manifest.json, catalog.json,
                    # run_results.json) uploaded by DbtS3ArtifactsResource after
                    # each full Dagster dbt build.
                    "Sid": "S3DagsterArtifactsRead",
                    "Effect": "Allow",
                    "Action": [
                        "s3:GetObject",
                        "s3:ListBucket",
                    ],
                    "Resource": [
                        f"arn:aws:s3:::dagster-data-{stack_info.env_suffix}",
                        f"arn:aws:s3:::dagster-data-{stack_info.env_suffix}/*",
                    ],
                },
                {
                    # Allows IAM-based auth to any RDS instance for the
                    # read_only_role DB user. Works in combination with
                    # GRANT rds_iam TO "read_only_role" in vault.py
                    # (applied to all Vault-managed PostgreSQL databases).
                    "Sid": "RDSIAMAuth",
                    "Effect": "Allow",
                    "Action": ["rds-db:connect"],
                    "Resource": [
                        f"arn:aws:rds-db:{aws_region}:{aws_account.account_id}:dbuser:*/read_only_role"
                    ],
                },
            ],
        },
        stringify=True,
        parliament_config={
            "RESOURCE_EFFECTIVELY_STAR": {},
            "CREDENTIALS_EXPOSURE": {},
            "PERMISSIONS_MANAGEMENT_ACTIONS": {},
        },
    ),
    tags=aws_config.tags,
)

ingestion_irsa_role = OLEKSTrustRole(
    f"om-ingestion-irsa-role-{stack_info.env_suffix}",
    role_config=OLEKSTrustRoleConfig(
        account_id=aws_account.account_id,
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        description=(
            "IRSA trust role for OpenMetadata ingestion service account"
            " - Glue, S3, and RDS IAM auth"
        ),
        policy_operator="StringEquals",
        role_name=f"om-ingestion-{stack_info.env_suffix}",
        service_account_name=ingestion_sa_name,
        service_account_namespace=open_metadata_namespace,
        tags=aws_config.tags,
    ),
)

iam.RolePolicyAttachment(
    f"om-ingestion-policy-attachment-{stack_info.env_suffix}",
    policy_arn=ingestion_iam_policy.arn,
    role=ingestion_irsa_role.role.name,
    opts=ResourceOptions(parent=ingestion_irsa_role),
)

# Annotate the existing openmetadata-ingestion SA (created by the helm chart)
# with the IRSA ARN so pods can assume the role.
kubernetes.core.v1.ServiceAccountPatch(
    f"om-ingestion-sa-irsa-annotation-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaPatchArgs(
        name=ingestion_sa_name,
        namespace=open_metadata_namespace,
        annotations={
            "eks.amazonaws.com/role-arn": ingestion_irsa_role.role.arn,
        },
    ),
    opts=ResourceOptions(depends_on=[ingestion_irsa_role]),
)

# ---------------------------------------------------------------------------
# CronOMJob helpers
# ---------------------------------------------------------------------------

INGESTION_IMAGE = (
    f"docker.getcollate.io/openmetadata/ingestion-base:{OPEN_METADATA_VERSION}"
)
OM_SERVER_URL = "http://openmetadata:8585/api"

_POD_RESOURCES = {
    "requests": {"cpu": "500m", "memory": "1Gi"},
    "limits": {"cpu": "2", "memory": "4Gi"},
}
_POD_SECURITY_CONTEXT = {
    "runAsUser": 1000,
    "runAsGroup": 1000,
    "runAsNonRoot": True,
}


def _bot_jwt_env(bot_secret_name: str) -> dict[str, object]:
    """Return the env-var entry that injects the OM bot JWT from a K8s secret.

    :param bot_secret_name: Name of the K8s secret holding the bot token
        (e.g. ``"om-ingestion-bot"``, ``"om-lineage-bot"``).  The secret
        must have a key named ``OM_BOT_JWT_TOKEN`` (created by VSO in the
        application stack).
    """
    return {
        "name": "OM_BOT_JWT_TOKEN",
        "valueFrom": {
            "secretKeyRef": {
                "name": bot_secret_name,
                "key": "OM_BOT_JWT_TOKEN",
            }
        },
    }


def _secret_env(secret_name: str, key: str) -> dict[str, object]:
    return {
        "name": key,
        "valueFrom": {"secretKeyRef": {"name": secret_name, "key": key}},
    }


def _plain_env(name: str, value: "str | Output[str]") -> dict[str, object]:
    """Return an env-var entry with a literal (non-secret) value.

    Accepts a plain string or a Pulumi ``Output[str]`` — Pulumi resolves
    Outputs when serialising the CronOMJob spec.
    """
    return {"name": name, "value": value}


def _make_cronjob(  # noqa: PLR0913
    name: str,
    schedule: str,
    python_script: "str | Output[str]",
    extra_env: list[dict[str, object]] | None = None,
    opts: ResourceOptions | None = None,
    bot_secret_name: str = "om-ingestion-bot",  # noqa: S107
    resources: dict[str, object] | None = None,
) -> kubernetes.apiextensions.CustomResource:
    """Create a CronOMJob custom resource.

    :param name: Unique kebab-case name for the CronOMJob.
    :param schedule: Cron expression for the schedule.
    :param python_script: Complete Python script to run in the ingestion pod.
        Accepts a plain ``str`` or a Pulumi ``Output[str]`` (e.g. when the
        script embeds resolved stack outputs).  Should include ``import os``
        and a ``config = {...}`` dict that may reference ``os.environ[...]``
        for credential injection.
    :param extra_env: Additional env vars (e.g. secretKeyRef entries).
        The bot JWT env var (``OM_BOT_JWT_TOKEN``) is always prepended
        automatically, sourced from *bot_secret_name*.
    :param opts: Optional Pulumi resource options (e.g. depends_on).
    :param bot_secret_name: K8s secret that holds the OM bot JWT token.
        Defaults to ``"om-ingestion-bot"`` for metadata ingestion workflows.
        Use ``"om-lineage-bot"``, ``"om-profiler-bot"``, or
        ``"om-data-insight-bot"`` for other workflow types.
    :param resources: Pod resource requests/limits. Defaults to
        ``_POD_RESOURCES`` (500m/1Gi request, 2/4Gi limit). Override for
        connectors that require more memory (e.g. Trino full-catalog ingestion).
    """
    env = [_bot_jwt_env(bot_secret_name), *(extra_env or [])]
    command = Output.from_input(python_script).apply(lambda s: ["python", "-c", s])

    return kubernetes.apiextensions.CustomResource(
        f"om-cronjob-{name}-{stack_info.env_suffix}",
        api_version="pipelines.openmetadata.org/v1",
        kind="CronOMJob",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"om-ingest-{name}",
            namespace=open_metadata_namespace,
        ),
        spec={
            "schedule": schedule,
            "omJobSpec": {
                "mainPodSpec": {
                    "image": INGESTION_IMAGE,
                    "serviceAccountName": ingestion_sa_name,
                    "command": command,
                    "env": env,
                    "resources": resources or _POD_RESOURCES,
                    "securityContext": _POD_SECURITY_CONTEXT,
                },
            },
            "failedJobsHistoryLimit": 3,
            "successfulJobsHistoryLimit": 3,
        },
        opts=opts or ResourceOptions(depends_on=[ingestion_irsa_role]),
    )


# ---------------------------------------------------------------------------
# Connector service names (configurable per environment)
# ---------------------------------------------------------------------------
service_name_trino = om_config.get("service_name_trino") or "Starburst Galaxy"
service_name_airbyte = om_config.get("service_name_airbyte") or "Airbyte"
service_name_superset = om_config.get("service_name_superset") or "Superset"
service_name_glue = om_config.get("service_name_glue") or "Glue"
# dbt enrichment targets the Trino (Starburst Galaxy) service — dbt writes
# via Trino so the manifest database/schema values map directly to the Trino
# OM entity FQNs (Starburst Galaxy.<catalog>.<schema>.<table>).
# Can be overridden per-stack.
service_name_dbt = om_config.get("service_name_dbt") or service_name_trino
# Dagster pipeline service name in OM.
service_name_dagster = om_config.get("service_name_dagster") or "OL Orchestration"

# External Dagster UI URL — used as sourceUrl base for pipeline/task entities
# so links in OM are clickable by users.  Defaults to the standard env-specific
# pattern (pipelines.odl.mit.edu for Production, pipelines-{env}.odl.mit.edu
# for others) but can be overridden via open_metadata:dagster_external_url.
_dagster_domain_by_env = {
    "production": "pipelines.odl.mit.edu",
    "qa": "pipelines-qa.odl.mit.edu",
    "ci": "pipelines-ci.odl.mit.edu",
}
_dagster_default_domain = _dagster_domain_by_env.get(
    stack_info.env_suffix, "pipelines.odl.mit.edu"
)
dagster_external_url = (
    om_config.get("dagster_external_url") or f"https://{_dagster_default_domain}"
)

# ---------------------------------------------------------------------------
# Trino (Starburst Galaxy) metadata ingestion
# ---------------------------------------------------------------------------

_make_cronjob(
    name="trino",
    schedule="0 2 * * *",
    python_script=(Path(__file__).parent / "scripts" / "trino_metadata.py").read_text(),
    extra_env=[
        _plain_env("OM_SERVER_URL", OM_SERVER_URL),
        _plain_env("OM_SERVICE_NAME", service_name_trino),
        _secret_env("om-connector-trino", "OM_TRINO_HOST_PORT"),
        _secret_env("om-connector-trino", "OM_TRINO_USERNAME"),
        _secret_env("om-connector-trino", "OM_TRINO_PASSWORD"),
        _secret_env("om-connector-trino", "OM_TRINO_CATALOG"),
    ],
    # Trino full-catalog ingestion is memory-intensive; increase limits above
    # the shared default (4Gi) to avoid OOM kills.
    resources={
        "requests": {"cpu": "500m", "memory": "2Gi"},
        "limits": {"cpu": "2", "memory": "8Gi"},
    },
)

# ---------------------------------------------------------------------------
# Airbyte metadata ingestion
# ---------------------------------------------------------------------------
# Only created when the om-connector-airbyte K8s secret exists, which the
# application stack provisions only when airbyte credentials are present in
# the SOPS secrets file (src/bridge/secrets/open_metadata/secrets.<env>.yaml).

if om_config.get_bool("enable_airbyte_connector"):
    _make_cronjob(
        name="airbyte",
        schedule="0 3 * * *",
        python_script=(
            Path(__file__).parent / "scripts" / "airbyte_metadata.py"
        ).read_text(),
        extra_env=[
            _plain_env("OM_SERVER_URL", OM_SERVER_URL),
            _plain_env("OM_SERVICE_NAME", service_name_airbyte),
            _secret_env("om-connector-airbyte", "OM_AIRBYTE_HOST_PORT"),
            _secret_env("om-connector-airbyte", "OM_AIRBYTE_PIPELINE_URL"),
        ],
    )

# ---------------------------------------------------------------------------
# Superset metadata ingestion
# ---------------------------------------------------------------------------
# Authenticates via an OIDC client_credentials grant (Keycloak).  Superset is
# configured with AUTH_TYPE=AUTH_OAUTH and validates Bearer tokens using the
# Keycloak realm public key (JWT_PUBLIC_KEY), so no FAB /security/login step
# is needed.  OIDC credentials are stored in the om-connector-superset K8s
# secret, provisioned by the open_metadata application stack via Vault.

superset_url = superset_stack.require_output("superset")["url"]

_make_cronjob(
    name="superset",
    schedule="0 4 * * *",
    python_script=(
        Path(__file__).parent / "scripts" / "superset_metadata.py"
    ).read_text(),
    extra_env=[
        _plain_env("OM_SERVER_URL", OM_SERVER_URL),
        _plain_env("OM_SERVICE_NAME", service_name_superset),
        _plain_env("OM_SUPERSET_URL", superset_url),
        _secret_env("om-connector-superset", "OM_SUPERSET_OIDC_REALM_URL"),
        _secret_env("om-connector-superset", "OM_SUPERSET_OIDC_CLIENT_ID"),
        _secret_env("om-connector-superset", "OM_SUPERSET_OIDC_CLIENT_SECRET"),
    ],
    opts=ResourceOptions(depends_on=[ingestion_irsa_role]),
)

# ---------------------------------------------------------------------------
# Trino (Starburst Galaxy) lineage extraction
# ---------------------------------------------------------------------------
# Starburst Galaxy's 30-day query history is in
# galaxy_telemetry.public.query_history, but its column names differ from
# the standard Trino system.runtime.queries view that OM's built-in
# TrinoLineageSource expects:
#
#   OM expected         Galaxy telemetry
#   ──────────────────  ─────────────────────
#   "query"             query          (same)
#   "user"              email
#   "started"           create_time
#   "end"               end_time
#   "state" = FINISHED  query_state = FINISHED
#
# TrinoLineageSource reads its SQL from the class attribute `sql_stmt` and
# appends lineage-specific filters from `filters`. We patch both before
# constructing the workflow so all SQL parsing, OM entity resolution, and
# lineage posting logic remains intact.
#
# Lineage for Iceberg-backed tables is also captured here since Trino is
# the query engine that reads and writes them.

_make_cronjob(
    name="trino-lineage",
    schedule="0 6 * * *",
    python_script=(Path(__file__).parent / "scripts" / "trino_lineage.py").read_text(),
    extra_env=[
        _plain_env("OM_SERVER_URL", OM_SERVER_URL),
        _plain_env("OM_SERVICE_NAME", service_name_trino),
        _secret_env("om-connector-trino", "OM_TRINO_HOST_PORT"),
        _secret_env("om-connector-trino", "OM_TRINO_USERNAME"),
        _secret_env("om-connector-trino", "OM_TRINO_PASSWORD"),
        _secret_env("om-connector-trino", "OM_TRINO_CATALOG"),
    ],
    bot_secret_name="om-lineage-bot",  # noqa: S106  # pragma: allowlist secret
)

# ---------------------------------------------------------------------------
# Glue Data Catalog metadata ingestion
# ---------------------------------------------------------------------------
# Uses IRSA for Glue API access — no credentials secret needed.
# Reads all metadata from the Glue API (boto3) without fetching Iceberg
# metadata files from S3, providing resilience to stale __dbt_tmp Glue
# entries and additionally capturing locationPath, fileFormat, table-type
# classification, and external-table → S3 container lineage.

_make_cronjob(
    name="glue",
    schedule="0 2 * * *",
    python_script=(Path(__file__).parent / "scripts" / "glue_metadata.py").read_text(),
    extra_env=[
        _plain_env("OM_SERVER_URL", OM_SERVER_URL),
        _plain_env("OM_SERVICE_NAME", service_name_glue),
        _plain_env("OM_AWS_REGION", aws_region),
    ],
)

# ---------------------------------------------------------------------------
# dbt metadata enrichment
# ---------------------------------------------------------------------------
# Runs after Glue metadata (2 AM) so that the Glue service tables already
# exist in OM before dbt descriptions, tags, and test results are applied.
#
# Downloads manifest.json, catalog.json, and run_results.json from the
# Dagster S3 bucket (dagster-data-{env}) at the prefix uploaded by
# DbtS3ArtifactsResource after each full dbt build in the lakehouse code
# location.  IRSA provides ambient S3 credentials — no secret needed.
#
# The serviceName must match the Trino service so that dbt model FQNs
# (Starburst Galaxy.<catalog>.<schema>.<table>) resolve to the correct
# table entities already ingested by the Trino connector.

# Bucket where DbtS3ArtifactsResource uploads artifacts; can be overridden
# per-stack via the open_metadata:dbt_artifacts_bucket config key.
dbt_artifacts_bucket = (
    om_config.get("dbt_artifacts_bucket") or f"dagster-data-{stack_info.env_suffix}"
)

_make_cronjob(
    name="dbt",
    schedule="0 3 * * *",
    python_script=(Path(__file__).parent / "scripts" / "dbt_metadata.py").read_text(),
    extra_env=[
        _plain_env("OM_SERVER_URL", OM_SERVER_URL),
        _plain_env("OM_SERVICE_NAME", service_name_dbt),
        _plain_env("OM_AWS_REGION", aws_region),
        _plain_env("OM_DBT_BUCKET", dbt_artifacts_bucket),
    ],
)

# ---------------------------------------------------------------------------
# Dagster pipeline metadata ingestion
# ---------------------------------------------------------------------------
# Custom ingestion that replaces the manually-created Dagster OMJob.
# Improvements:
#   - Source URLs point to the external Dagster UI (not the internal K8s
#     service address), so links in OM are clickable by users.
#   - One OM Pipeline per asset *group* (per code location) instead of one
#     per Dagster job, so dbt model layers, Airbyte sync groups, and other
#     logical collections each appear as a named pipeline rather than all
#     collapsing into the synthetic __ASSET_JOB entity.
#   - OM Tasks are asset nodes (with asset-page source URLs), not op handles.
#   - Inter-group asset dependencies become Pipeline → Pipeline lineage edges.
#   - Groups covered by other connectors (Superset datasets) are skipped.

_make_cronjob(
    name="dagster",
    schedule="0 5 * * *",
    python_script=(
        Path(__file__).parent / "scripts" / "dagster_metadata.py"
    ).read_text(),
    extra_env=[
        _plain_env("OM_SERVER_URL", OM_SERVER_URL),
        _plain_env("DAGSTER_SERVICE_NAME", service_name_dagster),
        _plain_env("DAGSTER_EXTERNAL_URL", dagster_external_url),
    ],
)

# Glue ↔ Trino table lineage — creates bidirectional lineage edges between
# Glue and Trino (Starburst Galaxy) entities that represent the same underlying
# Iceberg tables.  Runs daily at 02:30 UTC, after both metadata jobs (02:00)
# and before the dbt enrichment job (03:00) so dbt lineage resolution sees
# complete table lineage.
_make_cronjob(
    name="glue-trino-lineage",
    schedule="30 2 * * *",
    python_script=(
        Path(__file__).parent / "scripts" / "glue_trino_lineage.py"
    ).read_text(),
    extra_env=[
        _plain_env("OM_SERVER_URL", OM_SERVER_URL),
        _plain_env("OM_GLUE_SERVICE_NAME", service_name_glue),
        _plain_env("OM_TRINO_SERVICE_NAME", service_name_trino),
    ],
    bot_secret_name="om-lineage-bot",  # noqa: S106  # pragma: allowlist secret
)


# distinct %, min/max, mean/std) for all ol_warehouse_production_* schemas.
# Uses a 10% row sample; runs weekly (Sunday 07:00 UTC) to control cost.
_make_cronjob(
    name="trino-profiler",
    schedule="0 7 * * 0",
    python_script=(Path(__file__).parent / "scripts" / "trino_profiler.py").read_text(),
    extra_env=[
        _plain_env("OM_SERVER_URL", OM_SERVER_URL),
        _plain_env("OM_SERVICE_NAME", service_name_trino),
        _secret_env("om-connector-trino", "OM_TRINO_HOST_PORT"),
        _secret_env("om-connector-trino", "OM_TRINO_USERNAME"),
        _secret_env("om-connector-trino", "OM_TRINO_PASSWORD"),
        _secret_env("om-connector-trino", "OM_TRINO_CATALOG"),
    ],
    bot_secret_name="om-profiler-bot",  # noqa: S106  # pragma: allowlist secret
    # Profiler queries all production schemas with 10% sample; increase memory
    # above default 4Gi to avoid OOM kills observed in recent runs (exit 137).
    resources={
        "requests": {"cpu": "500m", "memory": "2Gi"},
        "limits": {"cpu": "2", "memory": "8Gi"},
    },
)

# Trino PII auto-classifier — uses OM's built-in AutoClassificationWorkflow,
# which combines column-name regex (ColumnNameScanner) and Presidio NLP on
# sampled rows (NERScanner) to suggest PII.Sensitive / PII.NonSensitive tags.
# Suggested tags propagate via OM lineage once a data steward confirms them.
# Runs weekly (Sunday 08:00 UTC, after the profiler).
_make_cronjob(
    name="trino-classifier",
    schedule="0 8 * * 0",
    python_script=(
        Path(__file__).parent / "scripts" / "trino_classifier.py"
    ).read_text(),
    extra_env=[
        _plain_env("OM_SERVER_URL", OM_SERVER_URL),
        _plain_env("OM_SERVICE_NAME", service_name_trino),
        _secret_env("om-connector-trino", "OM_TRINO_HOST_PORT"),
        _secret_env("om-connector-trino", "OM_TRINO_USERNAME"),
        _secret_env("om-connector-trino", "OM_TRINO_PASSWORD"),
        _secret_env("om-connector-trino", "OM_TRINO_CATALOG"),
    ],
    bot_secret_name="om-profiler-bot",  # noqa: S106  # pragma: allowlist secret
)

# ---------------------------------------------------------------------------
# Server-side configuration bootstrap
# ---------------------------------------------------------------------------
# Applies OM server settings that cannot be expressed in connector workflow
# configs (e.g. autoClassificationEnabled on PII / PersonalData tags).
#
# Uses a regular Kubernetes Job (not a CronOMJob) because this is a
# one-time-per-change operation, not a recurring ingestion workflow.  The
# Kubernetes Job name embeds a content hash of the script so Pulumi
# recreates it — and re-runs it — automatically whenever om_server_config.py
# changes.  ResourceOptions(delete_before_replace=True) ensures the old Job
# is deleted before the new one is created (K8s Jobs are immutable once
# running).
_server_config_script = (
    Path(__file__).parent / "scripts" / "om_server_config.py"
).read_text()
_server_config_hash = hashlib.sha256(_server_config_script.encode()).hexdigest()[:8]

kubernetes.batch.v1.Job(
    f"om-server-config-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=f"om-server-config-{_server_config_hash}",
        namespace=open_metadata_namespace,
    ),
    spec=kubernetes.batch.v1.JobSpecArgs(
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels={"app": "om-server-config"},
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                restart_policy="OnFailure",
                service_account_name=ingestion_sa_name,
                security_context=kubernetes.core.v1.PodSecurityContextArgs(
                    run_as_user=1000,
                    run_as_group=1000,
                    run_as_non_root=True,
                ),
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="om-server-config",
                        image=INGESTION_IMAGE,
                        command=["python", "-c", _server_config_script],
                        env=[
                            kubernetes.core.v1.EnvVarArgs(
                                name="OM_BOT_JWT_TOKEN",
                                value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                    secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                        name="om-ingestion-bot",
                                        key="OM_BOT_JWT_TOKEN",
                                    )
                                ),
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="OM_SERVER_URL",
                                value=OM_SERVER_URL,
                            ),
                        ],
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={"cpu": "100m", "memory": "128Mi"},
                            limits={"cpu": "500m", "memory": "256Mi"},
                        ),
                    )
                ],
            ),
        ),
        backoff_limit=3,
        ttl_seconds_after_finished=86400,  # auto-clean after 24 h
    ),
    opts=ResourceOptions(
        depends_on=[ingestion_irsa_role],
        delete_before_replace=True,
    ),
)

export(
    "open_metadata_ingestion",
    {
        "ingestion_irsa_role_arn": ingestion_irsa_role.role.arn,
    },
)
