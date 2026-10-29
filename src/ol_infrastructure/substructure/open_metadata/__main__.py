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
import json
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import (
    Config,
    InvokeOptions,
    Output,
    ResourceOptions,
    StackReference,
    export,
)
from pulumi_aws import get_caller_identity, iam

from bridge.lib.versions import OPEN_METADATA_VERSION
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.aws.eks import OLEKSTrustRole, OLEKSTrustRoleConfig
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import setup_k8s_provider
from ol_infrastructure.lib.aws.iam_helper import (
    IAM_POLICY_VERSION,
    cross_environment_glue_denial,
    data_lake_glue_resources,
    lint_iam_policy,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack, stack_ref
from ol_infrastructure.lib.vault import setup_vault_provider
from ol_infrastructure.providers.openmetadata import (
    AlertArgument,
    AlertCondition,
    OpenMetadataBot,
    OpenMetadataBotSpec,
    OpenMetadataConnection,
    OpenMetadataEventSubscription,
    OpenMetadataEventSubscriptionSpec,
)
from ol_infrastructure.providers.openmetadata.access_control import (
    OpenMetadataPolicy,
    OpenMetadataRole,
    OpenMetadataTeam,
)
from ol_infrastructure.substructure.keycloak.openmetadata_provisioner import (
    OPENMETADATA_PROVISIONER_VAULT_MOUNT,
    OPENMETADATA_PROVISIONER_VAULT_NAME,
)
from ol_infrastructure.substructure.open_metadata.access_control import (
    POLICIES,
    ROLES,
    TEAMS,
)

stack_info = parse_stack()
om_config = Config("open_metadata")

aws_region = om_config.get("aws_region") or "us-east-1"
aws_account = get_caller_identity()
aws_config = AWSBase(
    tags={"OU": "data", "Environment": f"data-{stack_info.env_suffix}"},
)

cluster_stack = StackReference(stack_ref(projects.EKS, f"data.{stack_info.name}"))
superset_stack = StackReference(stack_ref(projects.SUPERSET, stack_info.name))
open_metadata_stack = StackReference(stack_ref(projects.OPEN_METADATA, stack_info.name))

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))
vault_provider = setup_vault_provider()

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
                    "Resource": data_lake_glue_resources(stack_info.env_suffix),
                },
                *cross_environment_glue_denial(stack_info.env_suffix),
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

# Pipeline RBAC for k8s ingestion jobs.
#
# The OpenMetadata Helm chart can create these via k8s-pipeline-rbac.yaml, but it
# stamps the shared `serviceAccount.annotations` (the *server* IRSA role) onto the
# ingestion SA, colliding with the distinct ingestion role we need here. So the app
# stack sets `pipelineServiceClientConfig.k8s.rbac.enabled: false` and we own the
# full set of resources here, giving the ingestion SA its own IRSA role-arn and
# keeping clear lines of ownership between the two stacks.
#
# This mirrors the five resources from the chart's k8s-pipeline-rbac.yaml: the
# ingestion ServiceAccount + Role + RoleBinding, plus the server pipeline-manager
# Role + RoleBinding that lets the OpenMetadata server SA launch ingestion jobs.
server_sa_name = "openmetadata"

ingestion_service_account = kubernetes.core.v1.ServiceAccount(
    f"om-ingestion-sa-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=ingestion_sa_name,
        namespace=open_metadata_namespace,
        labels={"app.kubernetes.io/component": "ingestion"},
        annotations={
            "eks.amazonaws.com/role-arn": ingestion_irsa_role.role.arn,
        },
    ),
    opts=ResourceOptions(depends_on=[ingestion_irsa_role]),
)

# Core pipeline-job permissions shared by the ingestion SA and the server SA.
# The server additionally needs the OMJob/CronOMJob rules appended below. Keeping
# one source of truth avoids the two Roles drifting apart on a chart version bump.
pipeline_job_rules = [
    kubernetes.rbac.v1.PolicyRuleArgs(
        api_groups=[""],
        resources=["pods"],
        verbs=["get", "list", "create", "update", "patch", "delete"],
    ),
    kubernetes.rbac.v1.PolicyRuleArgs(
        api_groups=[""],
        resources=["pods/log"],
        verbs=["get", "list", "watch"],
    ),
    kubernetes.rbac.v1.PolicyRuleArgs(
        api_groups=[""],
        resources=["configmaps"],
        verbs=["get", "list", "create", "update", "patch", "delete"],
    ),
    kubernetes.rbac.v1.PolicyRuleArgs(
        api_groups=[""],
        resources=["secrets"],
        verbs=["get", "list", "create", "update", "patch", "delete"],
    ),
    kubernetes.rbac.v1.PolicyRuleArgs(
        api_groups=[""],
        resources=["events"],
        verbs=["get", "list"],
    ),
    kubernetes.rbac.v1.PolicyRuleArgs(
        api_groups=["batch"],
        resources=["jobs", "cronjobs"],
        verbs=["get", "list", "create", "update", "patch", "delete"],
    ),
]

# Permissions the ingestion SA needs to run pipeline jobs in its own namespace.
ingestion_role = kubernetes.rbac.v1.Role(
    f"om-ingestion-role-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=ingestion_sa_name,
        namespace=open_metadata_namespace,
        labels={"app.kubernetes.io/component": "ingestion"},
    ),
    rules=pipeline_job_rules,
)

kubernetes.rbac.v1.RoleBinding(
    f"om-ingestion-rolebinding-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=ingestion_sa_name,
        namespace=open_metadata_namespace,
        labels={"app.kubernetes.io/component": "ingestion"},
    ),
    subjects=[
        kubernetes.rbac.v1.SubjectArgs(
            kind="ServiceAccount",
            name=ingestion_sa_name,
            namespace=open_metadata_namespace,
        )
    ],
    role_ref=kubernetes.rbac.v1.RoleRefArgs(
        kind="Role",
        name=ingestion_sa_name,
        api_group="rbac.authorization.k8s.io",
    ),
    opts=ResourceOptions(depends_on=[ingestion_service_account, ingestion_role]),
)

# Permissions the OpenMetadata server SA needs to manage pipeline jobs and the
# OMJob/CronOMJob custom resources used by the k8s pipeline client.
server_pipeline_manager_role = kubernetes.rbac.v1.Role(
    f"om-server-pipeline-manager-role-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="openmetadata-server-pipeline-manager",
        namespace=open_metadata_namespace,
        labels={"app.kubernetes.io/component": "server"},
    ),
    rules=[
        *pipeline_job_rules,
        kubernetes.rbac.v1.PolicyRuleArgs(
            api_groups=["pipelines.openmetadata.org"],
            resources=["omjobs"],
            verbs=["get", "list", "create", "update", "patch", "delete"],
        ),
        kubernetes.rbac.v1.PolicyRuleArgs(
            api_groups=["pipelines.openmetadata.org"],
            resources=["omjobs/status"],
            verbs=["get", "patch"],
        ),
        kubernetes.rbac.v1.PolicyRuleArgs(
            api_groups=["pipelines.openmetadata.org"],
            resources=["cronomjobs"],
            verbs=["get", "list", "create", "update", "patch", "delete"],
        ),
        kubernetes.rbac.v1.PolicyRuleArgs(
            api_groups=["pipelines.openmetadata.org"],
            resources=["cronomjobs/status"],
            verbs=["get", "patch"],
        ),
    ],
)

kubernetes.rbac.v1.RoleBinding(
    f"om-server-pipeline-manager-rolebinding-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="openmetadata-server-pipeline-manager",
        namespace=open_metadata_namespace,
        labels={"app.kubernetes.io/component": "server"},
    ),
    subjects=[
        kubernetes.rbac.v1.SubjectArgs(
            kind="ServiceAccount",
            name=server_sa_name,
            namespace=open_metadata_namespace,
        )
    ],
    role_ref=kubernetes.rbac.v1.RoleRefArgs(
        kind="Role",
        name="openmetadata-server-pipeline-manager",
        api_group="rbac.authorization.k8s.io",
    ),
    opts=ResourceOptions(depends_on=[server_pipeline_manager_role]),
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

# The omjob-operator always runs an exit-handler pod after the main pod and
# dereferences exitHandlerSpec without a null check, so a CronOMJob without one
# ends every run as Failed ("Cannot invoke ...getEnv() because podSpec is
# null"). The operator injects pipelineStatus=success|failed from the main
# pod's exit code, and the exit handler's own exit code sets the final phase.
# This handler only turns that status back into an exit code, so phase
# reflects the ingestion result. It runs as the namespace default service
# account because it needs neither the ingestion IRSA role nor its RBAC.
_EXIT_HANDLER_SPEC = {
    "image": INGESTION_IMAGE,
    "command": ["sh", "-c", 'test "$pipelineStatus" = success'],
    "resources": {
        "requests": {"cpu": "10m", "memory": "16Mi"},
        "limits": {"cpu": "100m", "memory": "64Mi"},
    },
    "securityContext": _POD_SECURITY_CONTEXT,
}

# Enable JSON-structured logs from the Python ingestion-base container.
# Supported by the ingestion-base image; passed through as LOG_FORMAT env var.
_LOG_FORMAT_ENV = {"name": "LOG_FORMAT", "value": "json"}


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
    *,
    long_running: bool = False,
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
    :param long_running: Run the main pod on on-demand capacity and exempt it
        from Karpenter disruption. The operator never retries a main pod that
        disappears, so a spot interruption or consolidation ends the run as
        "Main pod not found after timeout". Use for jobs that run for hours.
    """
    main_pod_placement: dict[str, object] = (
        {
            "annotations": {"karpenter.sh/do-not-disrupt": "true"},
            "nodeSelector": {"karpenter.sh/capacity-type": "on-demand"},
        }
        if long_running
        else {}
    )
    env = [_bot_jwt_env(bot_secret_name), _LOG_FORMAT_ENV, *(extra_env or [])]
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
                    **main_pod_placement,
                },
                "exitHandlerSpec": _EXIT_HANDLER_SPEC,
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
service_name_starrocks = om_config.get("service_name_starrocks") or "StarRocks"
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
# configured with AUTH_TYPE=AUTH_OAUTH and validates Bearer tokens against the
# Keycloak realm JWKS, so no FAB /security/login step
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
#
# Disabled in QA: galaxy_telemetry is not attached to the QA Galaxy cluster,
# so every run fails with CATALOG_NOT_FOUND.

if om_config.require_bool("enable_trino_lineage"):
    _make_cronjob(
        name="trino-lineage",
        schedule="0 6 * * *",
        python_script=(
            Path(__file__).parent / "scripts" / "trino_lineage.py"
        ).read_text(),
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
dbt_artifacts_prefix = "openmetadata/dbt-artifacts"

# The production dbt layer schemas.  Restricting the dbt job to them keeps it
# from resolving dev-namespace model names that don't exist in OM; the domains
# job assigns tables in the same schemas.
LAKE_DBT_SCHEMA_INCLUDES = [
    r"ol_warehouse_[a-z]+_(dimensional|external|intermediate|irx|mart|migration|raw|reporting|staging)$",
    r"ol_data_lake_[a-z]+",
]

_make_cronjob(
    name="dbt",
    schedule="0 3 * * *",
    python_script=(Path(__file__).parent / "scripts" / "dbt_metadata.py").read_text(),
    extra_env=[
        _plain_env("OM_SERVER_URL", OM_SERVER_URL),
        _plain_env("OM_SERVICE_NAME", service_name_dbt),
        _plain_env("OM_AWS_REGION", aws_region),
        _plain_env("OM_DBT_BUCKET", dbt_artifacts_bucket),
        _plain_env("OM_DBT_PREFIX", dbt_artifacts_prefix),
        _plain_env("OM_DBT_SCHEMA_INCLUDES", json.dumps(LAKE_DBT_SCHEMA_INCLUDES)),
    ],
)

# ---------------------------------------------------------------------------
# Domains for what dbt does not describe
# ---------------------------------------------------------------------------
# The dbt job above sets the domain of each table dbt builds or declares.  This
# one applies the same rules (ol-data-platform src/ol_dbt/openmetadata_domains.yml,
# uploaded beside the manifest) to the lake tables dbt does not declare, and
# derives each Superset dataset's and dashboard's domain from the tables it
# reads.  Glue tables, pipelines and charts are left without domains; the
# script's docstring says why.
#
# Runs after the dbt job (03:00), which sets the table domains the datasets
# take, and the Superset job (04:00), which records the dataset lineage.
#
# Gated by the required open_metadata:enable_domain_assignment: it reads the
# rules the lakehouse uploads beside the manifest on each scheduled build, and
# QA has no scheduled build (its dbt artifacts come from hand-launched
# dbt_docs_artifacts_job runs), so in QA the rules file never appears.
if om_config.require_bool("enable_domain_assignment"):
    _make_cronjob(
        name="domains",
        schedule="30 5 * * *",
        python_script=(
            Path(__file__).parent / "scripts" / "domain_assignment.py"
        ).read_text(),
        extra_env=[
            _plain_env("OM_SERVER_URL", OM_SERVER_URL),
            # The service the dbt job enriches, so the tables it leaves to that
            # job are the ones that job sets.
            _plain_env("OM_LAKE_SERVICE_NAME", service_name_dbt),
            _plain_env("OM_SUPERSET_SERVICE_NAME", service_name_superset),
            _plain_env("OM_AWS_REGION", aws_region),
            _plain_env("OM_DBT_BUCKET", dbt_artifacts_bucket),
            _plain_env("OM_DBT_PREFIX", dbt_artifacts_prefix),
            _plain_env("OM_DBT_SCHEMA_INCLUDES", json.dumps(LAKE_DBT_SCHEMA_INCLUDES)),
            # true logs each change without making it, e.g. to review a rules
            # change's effect before it lands.
            _plain_env(
                "OM_DOMAINS_DRY_RUN",
                json.dumps(om_config.get_bool("domains_dry_run", default=False)),
            ),
        ],
    )

# ---------------------------------------------------------------------------
# StarRocks: metadata, dbt enrichment, and lineage from the lake
# ---------------------------------------------------------------------------
# Covers what StarRocks stores itself, i.e. the databases of default_catalog
# that the tag:starrocks dbt models build into.  The Iceberg tables it reads
# through its external catalogs are the lake tables the Trino and Glue
# services already hold, and are not ingested a second time.
#
# open_metadata:starrocks_host_port unset (a stack with no StarRocks cluster)
# creates none of the three jobs.  Credentials come from the
# om-connector-starrocks secret, a Vault-issued read-only user created by the
# application stack.
#
# The three run in order after b2b_analytics_starrocks_nightly, the Dagster
# schedule that builds the models at 06:00 UTC and uploads their manifest and
# run results:
#   08:00  metadata          creates the tables in OM
#   08:30  dbt enrichment    descriptions, tags, owners and test results
#   09:00  lake lineage      edges from the lake tables each model reads
#
# The last two read the uploaded manifest, so open_metadata:enable_starrocks_dbt
# creates them only where that schedule runs.  ol-data-platform registers it in
# production alone (SCHEDULE_ENVIRONMENTS in lakehouse), and a hand
# materialization in QA can't stand in for it: ol_warehouse_qa_dimensional held
# none of the models' base tables on 2026-10-20, so the asset fails its
# base-table check before dbt runs and uploads nothing.  Turn this on in QA
# together with that schedule.
starrocks_host_port = om_config.get("starrocks_host_port")

# Name of the single service-level database OM files the StarRocks databases
# under.  dbt-starrocks leaves a node's database empty, so nothing has to match
# it; the catalog name says where the tables live.
STARROCKS_OM_DATABASE_NAME = "default_catalog"

# The StarRocks databases the dbt project builds into (dbt_project.yml in
# ol-data-platform, the models tagged `starrocks`).  The StarRocks manifest
# also holds every other model of the project under schema names that do not
# exist in StarRocks; this keeps the dbt job from looking each of them up.
STARROCKS_DBT_SCHEMAS = ["b2b_analytics", "b2b_learner_records"]

if starrocks_host_port:
    _make_cronjob(
        name="starrocks",
        schedule="0 8 * * *",
        python_script=(
            Path(__file__).parent / "scripts" / "starrocks_metadata.py"
        ).read_text(),
        extra_env=[
            _plain_env("OM_SERVER_URL", OM_SERVER_URL),
            _plain_env("OM_SERVICE_NAME", service_name_starrocks),
            _plain_env("OM_STARROCKS_HOST_PORT", starrocks_host_port),
            _plain_env("OM_STARROCKS_DATABASE_NAME", STARROCKS_OM_DATABASE_NAME),
            _secret_env("om-connector-starrocks", "OM_STARROCKS_USERNAME"),
            _secret_env("om-connector-starrocks", "OM_STARROCKS_PASSWORD"),
        ],
    )

if starrocks_host_port and om_config.require_bool("enable_starrocks_dbt"):
    starrocks_dbt_artifacts_prefix = f"{dbt_artifacts_prefix}/starrocks"

    # Same script as the Trino dbt job, pointed at the StarRocks artifacts.
    # The lake schemas are left out of the filter on purpose: the models'
    # sources live there, and the dbt workflow would otherwise treat each
    # source as the data model of the lake table it resolves to.  The lineage
    # job below writes those edges.
    _make_cronjob(
        name="dbt-starrocks",
        schedule="30 8 * * *",
        python_script=(
            Path(__file__).parent / "scripts" / "dbt_metadata.py"
        ).read_text(),
        extra_env=[
            _plain_env("OM_SERVER_URL", OM_SERVER_URL),
            _plain_env("OM_SERVICE_NAME", service_name_starrocks),
            _plain_env("OM_AWS_REGION", aws_region),
            _plain_env("OM_DBT_BUCKET", dbt_artifacts_bucket),
            _plain_env("OM_DBT_PREFIX", starrocks_dbt_artifacts_prefix),
            _plain_env(
                "OM_DBT_SCHEMA_INCLUDES",
                json.dumps([f"^{schema}$" for schema in STARROCKS_DBT_SCHEMAS]),
            ),
        ],
    )

    _make_cronjob(
        name="starrocks-dbt-lineage",
        schedule="0 9 * * *",
        python_script=(
            Path(__file__).parent / "scripts" / "starrocks_dbt_lineage.py"
        ).read_text(),
        extra_env=[
            _plain_env("OM_SERVER_URL", OM_SERVER_URL),
            _plain_env("OM_STARROCKS_SERVICE_NAME", service_name_starrocks),
            _plain_env("OM_STARROCKS_DATABASE_NAME", STARROCKS_OM_DATABASE_NAME),
            _plain_env("OM_LAKE_SERVICE_NAME", service_name_trino),
            _plain_env("OM_AWS_REGION", aws_region),
            _plain_env("OM_DBT_BUCKET", dbt_artifacts_bucket),
            _plain_env("OM_DBT_PREFIX", starrocks_dbt_artifacts_prefix),
            _plain_env("OM_DBT_SCHEMAS", json.dumps(STARROCKS_DBT_SCHEMAS)),
            # The job deletes the lake edges the manifest no longer declares.
            # It neither links nor prunes when a run would delete more than
            # this share of them, since a manifest from the wrong target or a
            # broken upload looks like every model stopped reading the lake.
            # Raise it for one run after a refactor that legitimately drops
            # that many.
            _plain_env(
                "OM_LINEAGE_PRUNE_MAX_FRACTION",
                str(
                    om_config.get_float(
                        "starrocks_lineage_prune_max_fraction", default=0.5
                    )
                ),
            ),
            _plain_env(
                "OM_LINEAGE_PRUNE_DRY_RUN",
                json.dumps(
                    om_config.get_bool("starrocks_lineage_prune_dry_run", default=False)
                ),
            ),
        ],
        bot_secret_name="om-lineage-bot",  # noqa: S106  # pragma: allowlist secret
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


# Trino data profiler: collects table and column statistics (row count, null %,
# distinct %, min/max, mean/std) for the schemas matching the stack's
# trino_profiler_schema_pattern. QA reads the production catalog on a smaller
# Galaxy cluster, so it profiles one small schema rather than all of them.
# Uses a 10% row sample; runs weekly (Sunday 07:00 UTC) to control cost.
_make_cronjob(
    name="trino-profiler",
    schedule="0 7 * * 0",
    python_script=(Path(__file__).parent / "scripts" / "trino_profiler.py").read_text(),
    extra_env=[
        _plain_env("OM_SERVER_URL", OM_SERVER_URL),
        _plain_env("OM_SERVICE_NAME", service_name_trino),
        _plain_env(
            "OM_PROFILER_SCHEMA_PATTERN",
            om_config.require("trino_profiler_schema_pattern"),
        ),
        _secret_env("om-connector-trino", "OM_TRINO_HOST_PORT"),
        _secret_env("om-connector-trino", "OM_TRINO_USERNAME"),
        _secret_env("om-connector-trino", "OM_TRINO_PASSWORD"),
        _secret_env("om-connector-trino", "OM_TRINO_CATALOG"),
    ],
    bot_secret_name="om-profiler-bot",  # noqa: S106  # pragma: allowlist secret
    # Production profiles every warehouse schema with a 10% sample; increase memory
    # above default 4Gi to avoid OOM kills observed in recent runs (exit 137).
    resources={
        "requests": {"cpu": "500m", "memory": "2Gi"},
        "limits": {"cpu": "2", "memory": "8Gi"},
    },
    # A full pass takes about 28h. The scheduled runs on 2026-09-27, 10-04 and
    # 10-11 were all lost to spot interruptions.
    long_running=True,
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
# Access control: policies, roles and teams
# ---------------------------------------------------------------------------
# Declared in access_control.py. These and the bots run as the Keycloak service
# account that the application stack lists in initialAdmins (substructure/keycloak
# openmetadata_provisioner.py), not as the ingestion bot the Jobs use: assigning
# roles and editing display names need an admin.
_provisioner_credentials = vault.kv.get_secret_v2_output(
    mount=OPENMETADATA_PROVISIONER_VAULT_MOUNT,
    name=OPENMETADATA_PROVISIONER_VAULT_NAME,
    # The stack transformation sets the provider on resources only; an invoke
    # without one would use the default provider, which has no Vault token.
    opts=InvokeOptions(provider=vault_provider),
).data
om_api_url = open_metadata_stack.require_output("open_metadata")["api_url"]
om_connection = OpenMetadataConnection(
    server_url=om_api_url,
    token_url=_provisioner_credentials["token_url"],
    client_id=_provisioner_credentials["client_id"],
    client_secret=_provisioner_credentials["client_secret"],
)

# Every resource here adopts its entity by name, so two Pulumi resources can
# hold the same OpenMetadata id: renaming a resource's Pulumi name (or giving it
# a parent) without an alias creates the new one by adopting the entity, then
# deletes the old one by id. Each is therefore retain_on_delete, as the Job that
# managed them never deleted anything. Remove a policy, role or team from
# OpenMetadata by hand after dropping it here.
om_policies = {
    spec.name: OpenMetadataPolicy(
        f"om-policy-{spec.name}-{stack_info.env_suffix}",
        connection=om_connection,
        spec=spec,
        opts=ResourceOptions(retain_on_delete=True),
    )
    for spec in POLICIES
}
# A role may also name a policy OpenMetadata ships, which has no resource here.
om_roles = {
    spec.name: OpenMetadataRole(
        f"om-role-{spec.name}-{stack_info.env_suffix}",
        connection=om_connection,
        spec=spec,
        opts=ResourceOptions(
            depends_on=[
                om_policies[name] for name in spec.policies if name in om_policies
            ],
            retain_on_delete=True,
        ),
    )
    for spec in ROLES
}
# Deleting a team would also take it off every asset it owns and the glossary
# it reviews.
om_teams = [
    OpenMetadataTeam(
        f"om-team-{spec.name}-{stack_info.env_suffix}",
        connection=om_connection,
        spec=spec,
        opts=ResourceOptions(
            depends_on=[
                om_roles[name] for name in spec.default_roles or [] if name in om_roles
            ],
            retain_on_delete=True,
        ),
    )
    for spec in TEAMS
]


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

server_config_job = kubernetes.batch.v1.Job(
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
        # The glossary the Job creates is owned by a team.
        depends_on=[ingestion_irsa_role, *om_teams],
        delete_before_replace=True,
    ),
)


# ---------------------------------------------------------------------------
# Bots
# ---------------------------------------------------------------------------
# Creating a bot, giving it roles and reading its token are admin-only in
# OpenMetadata. The bots hold roles declared above, so each waits for its role.

# The server names its own bots' users <bot>@<principalDomain>
# (BotResource.initialize); these follow suit.
_BOT_EMAIL_DOMAIN = "mit.edu"

# dagster-bot writes the Dagster asset check results, so that an OpenMetadata
# alert on test case failures can leave them out with matchUpdatedBy (Dagster
# notifies for its own checks). OpenMetadata 2.0.5's alert filters cannot match
# on a test case's platform or definition.
dagster_bot = OpenMetadataBot(
    f"om-dagster-bot-{stack_info.env_suffix}",
    connection=om_connection,
    spec=OpenMetadataBotSpec(
        name="dagster-bot",
        display_name="Dagster",
        description=(
            "Writes Dagster asset check results to OpenMetadata as test cases."
            " Managed by Pulumi in ol-infrastructure substructure/open_metadata."
        ),
        email=f"dagster-bot@{_BOT_EMAIL_DOMAIN}",
        roles=["DagsterBotRole"],
    ),
    opts=ResourceOptions(depends_on=[om_roles["DagsterBotRole"]]),
)

# ol-data-platform's lakehouse code location reads secret-data/pipelines/*. A
# path of its own rather than pipelines/openmetadata, which the application
# stack filled with the ingestion bot's token until Dagster moved here: two
# stacks writing one path would each delete it when they stop.
vault.generic.Secret(
    f"om-dagster-bot-vault-secret-{stack_info.env_suffix}",
    path="secret-data/pipelines/openmetadata-dagster-bot",
    data_json=Output.json_dumps({"token": dagster_bot.token, "server_url": om_api_url}),
)

# metrics-sync-bot runs ol-data-platform's `ol-dbt metrics sync`, which takes
# its own name from /v1/users/loggedInUser and reads drift against the versions
# that name wrote, so it cannot share another bot's identity. It is not a metric
# reviewer and has no team (ol-data-platform metrics/README.md, "Status and
# approval").
metrics_sync_bot = OpenMetadataBot(
    f"om-metrics-sync-bot-{stack_info.env_suffix}",
    connection=om_connection,
    spec=OpenMetadataBotSpec(
        name="metrics-sync-bot",
        display_name="Metrics sync",
        description=(
            "Publishes ol-data-platform's metrics/ files as Metric entities."
            " Managed by Pulumi in ol-infrastructure substructure/open_metadata."
        ),
        email=f"metrics-sync-bot@{_BOT_EMAIL_DOMAIN}",
        roles=["MetricsSyncBotRole"],
    ),
    opts=ResourceOptions(depends_on=[om_roles["MetricsSyncBotRole"]]),
)

# Beside the contracts sync's token, since the metrics sync is meant to run from
# Concourse the same way, and Concourse reads only secret-concourse/<team>/*.
# The developer policy reads secret-*, so the QA token also serves a manual
# `--dry-run`.
vault.generic.Secret(
    f"om-metrics-sync-bot-vault-secret-{stack_info.env_suffix}",
    path="secret-concourse/infrastructure/openmetadata_metrics_bot",
    data_json=Output.json_dumps(
        {"token": metrics_sync_bot.token, "server_url": om_api_url}
    ),
)


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
# A contract's status is the one result OpenMetadata alone produces: it fails on
# a missing contracted column or a failed semantics rule after ingestion. Test
# case failures are not alerted here, because Dagster already posts both kinds
# to the same channel: a dbt test failing past its error_if threshold fails its
# run (run failure sensor), and a failing Dagster check is posted by
# asset_check_failure_sensor. A dbt test that only warns is reported by neither.
#
# The filter matches every change event of a contract whose latest result is
# Failed, so a contract that stays failing is posted again on each daily
# validation. Aborted is left out: OpenMetadata sets it when a newer validation
# supersedes a running one or the quality run cannot be triggered, neither of
# which says the contract failed.
#
# OpenMetadata returns a Slack destination's endpoint unmasked to anyone with
# ViewAll on eventsubscription, which every user holds through the default
# policies, so the webhook URL is readable inside OpenMetadata.
#
# Each environment's webhook posts to the data platform Slack channel Dagster
# uses for that environment: notifications-data-platform in Production and
# notifications-data-platform-qa in QA.
_alert_secrets = read_yaml_secrets(
    Path(f"open_metadata/secrets.{stack_info.env_suffix}.yaml")
)

OpenMetadataEventSubscription(
    f"om-alert-data-contract-failures-{stack_info.env_suffix}",
    connection=om_connection,
    spec=OpenMetadataEventSubscriptionSpec(
        name="data-contract-failures",
        display_name="Data contract failures",
        description=(
            "Posts to Slack when a data contract's latest result is Failed."
            " Managed by Pulumi in ol-infrastructure substructure/open_metadata."
        ),
        alert_type="Observability",
        resource="dataContract",
        actions=[
            AlertCondition(
                name="GetDataContractStatusUpdates",
                arguments=[AlertArgument(name="contractStatusList", input=["Failed"])],
            )
        ],
    ),
    slack_webhook_url=_alert_secrets["slack_notifications_data_platform"],
)


# ---------------------------------------------------------------------------
# Team membership -> Keycloak groups
# ---------------------------------------------------------------------------
# Team membership is assigned in the OpenMetadata UI. This job mirrors each team
# declared in access_control.py's TEAMS into a Keycloak group under
# /openmetadata-teams in the ol-data-platform realm, adding and removing members
# to match. The Keycloak client and parent group come from the keycloak
# substructure stack through the om-team-keycloak-sync secret.
#
# A schedule rather than OpenMetadata change events: each run reconciles the
# whole state, so a missed run or a hand edit in Keycloak is corrected by the
# next one, and there is no webhook receiver to deploy and secure.
#
# OpenMetadata's jwtTeamClaimMapping stays unset. With it set, every login would
# add the Keycloak memberships back into OpenMetadata and undo removals made
# there.

_make_cronjob(
    name="team-keycloak-sync",
    schedule="17 * * * *",
    python_script=(
        Path(__file__).parent / "scripts" / "om_team_keycloak_sync.py"
    ).read_text(),
    extra_env=[
        _plain_env("OM_SERVER_URL", OM_SERVER_URL),
        _plain_env("OM_TEAMS", json.dumps(sorted(spec.name for spec in TEAMS))),
        *(
            _secret_env("om-team-keycloak-sync", key)
            for key in (
                "KEYCLOAK_REALM_URL",
                "KEYCLOAK_ADMIN_URL",
                "KEYCLOAK_CLIENT_ID",
                "KEYCLOAK_CLIENT_SECRET",
                "KEYCLOAK_PARENT_GROUP_ID",
            )
        ),
    ],
    resources={
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "256Mi"},
    },
)

export(
    "open_metadata_ingestion",
    {
        "ingestion_irsa_role_arn": ingestion_irsa_role.role.arn,
    },
)
