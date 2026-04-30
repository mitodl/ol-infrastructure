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

import json

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
    # Build the policy JSON inside an apply so the account_id Output is
    # resolved before serialization. lint_iam_policy cannot accept Outputs.
    policy=aws_account.account_id.apply(
        lambda acct_id: lint_iam_policy(
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
                        # Allows IAM-based auth to any RDS instance for the
                        # read_only_role DB user. Works in combination with
                        # GRANT rds_iam TO "read_only_role" in vault.py
                        # (applied to all Vault-managed PostgreSQL databases).
                        "Sid": "RDSIAMAuth",
                        "Effect": "Allow",
                        "Action": ["rds-db:connect"],
                        "Resource": [
                            f"arn:aws:rds-db:{aws_region}:{acct_id}:dbuser/*/read_only_role"
                        ],
                    },
                ],
            },
            stringify=True,
            parliament_config={"RESOURCE_EFFECTIVELY_STAR": {}},
        )
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
            " — Glue, S3, and RDS IAM auth"
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


def _make_cronjob(  # noqa: PLR0913
    name: str,
    schedule: str,
    python_script: "str | Output[str]",
    extra_env: list[dict[str, object]] | None = None,
    opts: ResourceOptions | None = None,
    bot_secret_name: str = "om-ingestion-bot",  # noqa: S107
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
                    "resources": _POD_RESOURCES,
                    "securityContext": _POD_SECURITY_CONTEXT,
                },
            },
            "ttlSecondsAfterFinished": 86400,
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
service_name_iceberg = om_config.get("service_name_iceberg") or "Iceberg"

# ---------------------------------------------------------------------------
# Trino (Starburst Galaxy) metadata ingestion
# ---------------------------------------------------------------------------

_make_cronjob(
    name="trino",
    schedule="0 2 * * *",
    python_script=f"""
import os
from metadata.workflow.metadata import MetadataWorkflow
config = {{
    "source": {{
        "type": "trino",
        "serviceName": {json.dumps(service_name_trino)},
        "serviceConnection": {{
            "config": {{
                "type": "Trino",
                "hostPort": os.environ["OM_TRINO_HOST_PORT"],
                "username": os.environ["OM_TRINO_USERNAME"],
                "authType": {{"password": os.environ["OM_TRINO_PASSWORD"]}},
                "catalog": os.environ["OM_TRINO_CATALOG"],
            }}
        }},
        "sourceConfig": {{"config": {{"type": "DatabaseMetadata"}}}},
    }},
    "sink": {{"type": "metadata-rest", "config": {{}}}},
    "workflowConfig": {{
        "openMetadataServerConfig": {{
            "hostPort": {json.dumps(OM_SERVER_URL)},
            "authProvider": "openmetadata",
            "securityConfig": {{"jwtToken": os.environ["OM_BOT_JWT_TOKEN"]}},
        }}
    }},
}}
workflow = MetadataWorkflow.create(config)
workflow.execute()
workflow.raise_from_status()
""",
    extra_env=[
        _secret_env("om-connector-trino", "OM_TRINO_HOST_PORT"),
        _secret_env("om-connector-trino", "OM_TRINO_USERNAME"),
        _secret_env("om-connector-trino", "OM_TRINO_PASSWORD"),
        _secret_env("om-connector-trino", "OM_TRINO_CATALOG"),
    ],
)

# ---------------------------------------------------------------------------
# Airbyte metadata ingestion
# ---------------------------------------------------------------------------

_make_cronjob(
    name="airbyte",
    schedule="0 3 * * *",
    python_script=f"""
import os
from metadata.workflow.metadata import MetadataWorkflow
config = {{
    "source": {{
        "type": "airbyte",
        "serviceName": {json.dumps(service_name_airbyte)},
        "serviceConnection": {{
            "config": {{
                "type": "Airbyte",
                "hostPort": os.environ["OM_AIRBYTE_HOST_PORT"],
                "username": os.environ["OM_AIRBYTE_USERNAME"],
                "password": os.environ["OM_AIRBYTE_PASSWORD"],
            }}
        }},
        "sourceConfig": {{"config": {{"type": "PipelineMetadata"}}}},
    }},
    "sink": {{"type": "metadata-rest", "config": {{}}}},
    "workflowConfig": {{
        "openMetadataServerConfig": {{
            "hostPort": {json.dumps(OM_SERVER_URL)},
            "authProvider": "openmetadata",
            "securityConfig": {{"jwtToken": os.environ["OM_BOT_JWT_TOKEN"]}},
        }}
    }},
}}
workflow = MetadataWorkflow.create(config)
workflow.execute()
workflow.raise_from_status()
""",
    extra_env=[
        _secret_env("om-connector-airbyte", "OM_AIRBYTE_HOST_PORT"),
        _secret_env("om-connector-airbyte", "OM_AIRBYTE_USERNAME"),
        _secret_env("om-connector-airbyte", "OM_AIRBYTE_PASSWORD"),
    ],
)

# ---------------------------------------------------------------------------
# Superset (PostgreSQL/RDS IAM auth) metadata ingestion
# ---------------------------------------------------------------------------
# Connects to the Superset RDS instance as the Vault-managed read_only_role DB
# user using IAM auth. No password secret needed — the ingestion SA's IRSA role
# grants rds-db:connect, and GRANT rds_iam TO "read_only_role" in vault.py
# enables IAM auth for any Vault-issued readonly user.

superset_db_host = superset_stack.require_output("superset")["db_host"]
superset_db_resource_id = superset_stack.require_output("superset")["db_resource_id"]

_superset_script = Output.all(
    host=superset_db_host,
    resource_id=superset_db_resource_id,
).apply(
    lambda args: (
        f"""
import os
from metadata.workflow.metadata import MetadataWorkflow
config = {{
    "source": {{
        "type": "superset",
        "serviceName": {json.dumps(service_name_superset)},
        "serviceConnection": {{
            "config": {{
                "type": "Superset",
                "connection": {{
                    "type": "PostgresConnection",
                    "username": "read_only_role",
                    "hostPort": {json.dumps(args["host"] + ":5432")},
                    "database": "superset",
                    "authType": {{
                        "awsConfig": {{"awsRegion": {json.dumps(aws_region)}}},
                        "iamMode": True,
                        "dbClusterIdentifier": {json.dumps(args["resource_id"])},
                    }},
                }},
            }}
        }},
        "sourceConfig": {{"config": {{"type": "DashboardMetadata"}}}},
    }},
    "sink": {{"type": "metadata-rest", "config": {{}}}},
    "workflowConfig": {{
        "openMetadataServerConfig": {{
            "hostPort": {json.dumps(OM_SERVER_URL)},
            "authProvider": "openmetadata",
            "securityConfig": {{"jwtToken": os.environ["OM_BOT_JWT_TOKEN"]}},
        }}
    }},
}}
workflow = MetadataWorkflow.create(config)
workflow.execute()
workflow.raise_from_status()
"""
    )
)

_make_cronjob(
    name="superset",
    schedule="0 4 * * *",
    python_script=_superset_script,
    opts=ResourceOptions(depends_on=[ingestion_irsa_role]),
)

# ---------------------------------------------------------------------------
# Iceberg (Glue + S3) metadata ingestion
# ---------------------------------------------------------------------------
# Uses IRSA for Glue/S3 access — no credentials secret needed.

_make_cronjob(
    name="iceberg",
    schedule="0 2 * * *",
    python_script=f"""
import os
from metadata.workflow.metadata import MetadataWorkflow
config = {{
    "source": {{
        "type": "iceberg",
        "serviceName": {json.dumps(service_name_iceberg)},
        "serviceConnection": {{
            "config": {{
                "type": "Iceberg",
                "catalog": {{
                    "type": "Glue",
                    "name": {json.dumps(service_name_iceberg)},
                    "connection": {{
                        "awsConfig": {{"awsRegion": {json.dumps(aws_region)}}},
                    }},
                }},
            }}
        }},
        "sourceConfig": {{"config": {{"type": "StorageMetadata"}}}},
    }},
    "sink": {{"type": "metadata-rest", "config": {{}}}},
    "workflowConfig": {{
        "openMetadataServerConfig": {{
            "hostPort": {json.dumps(OM_SERVER_URL)},
            "authProvider": "openmetadata",
            "securityConfig": {{"jwtToken": os.environ["OM_BOT_JWT_TOKEN"]}},
        }}
    }},
}}
workflow = MetadataWorkflow.create(config)
workflow.execute()
workflow.raise_from_status()
""",
    extra_env=[],
)

export(
    "open_metadata_ingestion",
    {
        "ingestion_irsa_role_arn": ingestion_irsa_role.role.arn,
    },
)
