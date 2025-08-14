"""Create the resources needed to run a airbyte server.  # noqa: D200"""

import json
import os
from pathlib import Path
from string import Template

import pulumi_consul as consul
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, Output, ResourceOptions, StackReference, export
from pulumi_aws import ec2, get_caller_identity, iam, s3
from pulumi_consul import Node, Service, ServiceCheckArgs

from bridge.lib.magic_numbers import (
    AWS_RDS_DEFAULT_DATABASE_CAPACITY,
    DEFAULT_HTTPS_PORT,
    DEFAULT_POSTGRES_PORT,
)
from bridge.lib.versions import AIRBYTE_CHART_VERSION, AIRBYTE_VERSION
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.eks import (
    OLEKSGateway,
    OLEKSGatewayConfig,
    OLEKSGatewayListenerConfig,
    OLEKSGatewayRouteConfig,
    OLEKSTrustRole,
    OLEKSTrustRoleConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import default_egress_args
from ol_infrastructure.lib.aws.eks_helper import setup_k8s_provider
from ol_infrastructure.lib.aws.iam_helper import (
    IAM_POLICY_VERSION,
    lint_iam_policy,
)
from ol_infrastructure.lib.consul import get_consul_provider
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import postgres_role_statements, setup_vault_provider

##################################
##    Setup + Config Retrival   ##
##################################

stack_info = parse_stack()
setup_vault_provider(stack_info)
airbyte_config = Config("airbyte")
vault_config = Config("vault")

network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(f"infrastructure.consul.data.{stack_info.name}")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
cluster_stack = StackReference(f"infrastructure.aws.eks.data.{stack_info.name}")

mitodl_zone_id = dns_stack.require_output("odl_zone_id")

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

target_vpc_name = airbyte_config.get("target_vpc") or f"{stack_info.env_prefix}_vpc"
target_vpc = network_stack.require_output(target_vpc_name)
k8s_pod_subnet_cidrs = target_vpc["k8s_pod_subnet_cidrs"]

consul_security_groups = consul_stack.require_output("security_groups")
aws_config = AWSBase(
    tags={
        "OU": airbyte_config.get("business_unit") or "operations",
        "Environment": f"{env_name}",
    }
)

k8s_global_labels = K8sGlobalLabels(
    service=Services.airbyte,
    ou=BusinessUnit.data,
    stack=stack_info,
).model_dump()
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))
airbyte_namespace = "airbyte"

aws_account = get_caller_identity()
vpc_id = target_vpc["id"]

consul_provider = get_consul_provider(stack_info)

VERSIONS = {
    "AIRBYTE_CHART": os.environ.get("AIRBYTE_CHART_VERSION", AIRBYTE_CHART_VERSION),
}

###############################
##     General Resources     ##
###############################

# S3 State Storage for Airbyte logs and system state
airbyte_bucket_name = f"ol-airbyte-{stack_info.env_suffix}"
s3.Bucket(
    "airbyte-state-storage-bucket",
    bucket=airbyte_bucket_name,
    tags=aws_config.tags,
)

# IAM and instance profile
airbyte_server_instance_role = iam.Role(
    f"airbyte-server-instance-role-{env_name}",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Principal": {"Service": "ec2.amazonaws.com"},
                },
            ],
        }
    ),
    path="/ol-infrastructure/airbyte-server/role/",
    tags=aws_config.tags,
)

parliament_config = {
    "PERMISSIONS_MANAGEMENT_ACTIONS": {
        "ignore_locations": [{"actions": ["s3:putobjectacl"]}]
    },
    "UNKNOWN_ACTION": {"ignore_locations": []},
    "RESOURCE_MISMATCH": {"ignore_locations": []},
    "UNKNOWN_CONDITION_FOR_ACTION": {"ignore_locations": []},
    "RESOURCE_STAR": {"ignore_locations": []},
}

airbyte_app_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject*",
                "s3:PutObject*",
                "s3:DeleteObject",
            ],
            "Resource": [f"arn:aws:s3:::{airbyte_bucket_name}/*"],
        },
        {
            "Effect": "Allow",
            "Action": "s3:ListBucket",
            "Resource": f"arn:aws:s3:::{airbyte_bucket_name}",
        },
        {
            "Effect": "Allow",
            "Action": [
                "secretsmanager:CreateSecret",
                "secretsmanager:DeleteSecret",
                "secretsmanager:DescribeSecret",
                "secretsmanager:GetSecretValue",
                "secretsmanager:ListSecrets",
                "secretsmanager:TagResource",
                "secretsmanager:UpdateSecret",
            ],
            "Resource": ["*"],
            "Condition": {
                "ForAllValues:StringEquals": {
                    "secretsmanager:ResourceTag/AirbyteManaged": "true"
                }
            },
        },
    ],
}
airbyte_app_policy = iam.Policy(
    "airbyte-app-instance-iam-policy",
    path=f"/ol-applications/airbyte-server/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    description=(
        "Grant access to AWS resources for the operation of the Airbyte application."
    ),
    policy=lint_iam_policy(
        airbyte_app_policy_document, stringify=True, parliament_config=parliament_config
    ),
    tags=aws_config.tags,
)

data_lake_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "s3:ListAllMyBuckets",
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject*",
                "s3:PutObject",
                "s3:PutObjectAcl",
                "s3:DeleteObject",
                "s3:ListBucket*",
            ],
            "Resource": [
                f"arn:aws:s3:::ol-data-lake-*-{stack_info.env_suffix}",
                f"arn:aws:s3:::ol-data-lake-*-{stack_info.env_suffix}/*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": [
                "glue:TagResource",
                "glue:UnTagResource",
            ],
            "Resource": ["*"],
        },
        {
            "Effect": "Allow",
            "Action": [
                "glue:BatchCreatePartition",
                "glue:BatchDeletePartition",
                "glue:BatchDeleteTable",
                "glue:BatchGetPartition",
                "glue:CreateDatabase",
                "glue:CreateTable",
                "glue:CreatePartition",
                "glue:DeletePartition",
                "glue:DeleteTable",
                "glue:GetDatabase",
                "glue:GetDatabases",
                "glue:GetPartition",
                "glue:GetPartitions",
                "glue:GetTable",
                "glue:GetTables",
                "glue:UpdateDatabase",
                "glue:UpdatePartition",
                "glue:UpdateTable",
            ],
            "Resource": [
                "arn:aws:glue:*:*:catalog",
                "arn:aws:glue:*:*:database/airbyte_test_namespace",
                "arn:aws:glue:*:*:table/airbyte_test_namespace/*",
                f"arn:aws:glue:*:*:database/*{stack_info.env_suffix}*",
                f"arn:aws:glue:*:*:table/*{stack_info.env_suffix}*/*",
            ],
        },
    ],
}
data_lake_policy = iam.Policy(
    "data-lake-access-policy",
    name_prefix="airbyte-datalake-policy-",
    path=f"/ol-applications/airbyte-server/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        data_lake_policy_document,
        stringify=True,
        parliament_config=parliament_config,
    ),
    description="AWS access permissions to allow airbyte to use ol-data-lake-* buckets",
)

# Create IAM policy for Airbyte to read from S3 source buckets
# TODO: Turn this into a stack reference after exporting the bucket names from the  # noqa: E501, FIX002, TD002
# edxapp Pulumi project. (TMM 2023-06-02)
s3_source_buckets = [
    f"{edxapp_deployment}-{stack_info.env_suffix}-edxapp-tracking"
    for edxapp_deployment in ("mitxonline", "mitx", "mitx-staging", "xpro")
]
s3_source_buckets.append(f"ol-data-lake-landing-zone-{stack_info.env_suffix}")

# This should use a reference to the monitoring stack but it seems broken at the moment
# and I can't figure it out
fastly_access_log_bucket_name = "mitodl-fastly-access-logs"
s3_source_buckets.append(fastly_access_log_bucket_name)

s3_source_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "s3:ListAllMyBuckets",
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject*",
                "s3:ListBucket*",
            ],
            "Resource": [
                f"arn:aws:s3:::{bucket_name}" for bucket_name in s3_source_buckets
            ]
            + [f"arn:aws:s3:::{bucket_name}/*" for bucket_name in s3_source_buckets],
        },
    ],
}
s3_source_policy = iam.Policy(
    "airbyte-s3-source-access-policy",
    name_prefix="airbyte-s3-source-policy-",
    path=f"/ol-applications/airbyte-server/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        s3_source_policy_document,
        stringify=True,
    ),
    description="AWS access permissions to access S3 buckets for data sources",
)

iam.RolePolicyAttachment(
    f"airbyte-server-describe-instance-role-policy-{env_name}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=airbyte_server_instance_role.name,
)
iam.RolePolicyAttachment(
    f"airbyte-server-route53-role-policy-{env_name}",
    policy_arn=policy_stack.require_output("iam_policies")["route53_odl_zone_records"],
    role=airbyte_server_instance_role.name,
)
iam.RolePolicyAttachment(
    f"airbyte-server-data-lake-access-policy-{env_name}",
    policy_arn=data_lake_policy.arn,
    role=airbyte_server_instance_role.name,
)
iam.RolePolicyAttachment(
    f"airbyte-server-s3-source-access-polic-{env_name}",
    policy_arn=s3_source_policy.arn,
    role=airbyte_server_instance_role.name,
)
iam.RolePolicyAttachment(
    "airbyte-app-instance-policy-attachement",
    policy_arn=airbyte_app_policy.arn,
    role=airbyte_server_instance_role.name,
)

airbyte_lakeformation_role = iam.Role(
    "airbyte-lakeformation-role",
    assume_role_policy=airbyte_server_instance_role.arn.apply(
        lambda instance_role_arn: json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "sts:AssumeRole",
                        "Principal": {"AWS": instance_role_arn},
                    },
                ],
            }
        )
    ),
    name=f"airbyte-lakeformation-role-{stack_info.env_suffix}",
    path="/ol-infrastructure/airbyte-app/role/",
    tags=aws_config.tags,
)

iam.RolePolicyAttachment(
    f"airbyte-application-lakeformation-access-policy-{env_name}",
    policy_arn=data_lake_policy.arn,
    role=airbyte_lakeformation_role.name,
)

# Vault policy definition
airbyte_server_vault_policy = vault.Policy(
    "airbyte-server-vault-policy",
    name="airbyte-server",
    policy=Path(__file__).parent.joinpath("airbyte_server_policy.hcl").read_text(),
)

airbyte_vault_k8s_auth_backend_role = vault.kubernetes.AuthBackendRole(
    "airbyte-vault-k8s-auth-backend-role",
    role_name="airbyte",
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    bound_service_account_names=["*"],
    bound_service_account_namespaces=[airbyte_namespace],
    token_policies=[airbyte_server_vault_policy.name],
)

# Create the secret mount used for storing configuration secrets
airbyte_vault_mount = vault.Mount(
    "airbyte-server-configuration-secrets-mount",
    path="secret-airbyte",
    type="kv-v2",
    options={"version": 2},
    description="Storage of configuration credentials used in Airbyte connections.",
    opts=ResourceOptions(delete_before_replace=True),
)

# Define a Vault role that can be used to generate credentials for the S3 source policy
vault.aws.SecretBackendRole(
    "airbyte-s3-source-vault-aws-role",
    name="airbyte-sources",
    backend="aws-mitx",
    credential_type="iam_user",
    iam_tags={"OU": "operations", "vault_managed": "True"},
    policy_arns=[s3_source_policy.arn],
)

airbyte_vault_secrets = read_yaml_secrets(
    Path(f"airbyte/data.{stack_info.env_suffix}.yaml")
)

vault.generic.Secret(
    "airbyte-server-configuration-sentry-secrets",
    path=airbyte_vault_mount.path.apply("{}/sentry-dsn".format),
    data_json=json.dumps(airbyte_vault_secrets["sentry-dsn"]),
)
##################################
#     Network Access Control     #
##################################
# Create security group
airbyte_server_security_group = ec2.SecurityGroup(
    f"airbyte-server-security-group-{env_name}",
    name=f"airbyte-server-{target_vpc_name}-{env_name}",
    description="Access control for airbyte servers",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_HTTPS_PORT,
            to_port=DEFAULT_HTTPS_PORT,
            cidr_blocks=["0.0.0.0/0"],
            description=(
                f"Allow traffic to the airbyte server on port {DEFAULT_HTTPS_PORT}"
            ),
        ),
    ],
    egress=default_egress_args,
    vpc_id=vpc_id,
)

airbyte_db_security_group = ec2.SecurityGroup(
    f"airbyte-db-security-group-{env_name}",
    name=f"airbyte-db-{target_vpc_name}-{env_name}",
    description="Access from airbyte to its own postgres database.",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                airbyte_server_security_group.id,
                consul_security_groups["consul_server"],
                vault_stack.require_output("vault_server")["security_group"],
            ],
            cidr_blocks=[target_vpc["cidr"]],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description="Access to Postgres from Airbyte nodes.",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=k8s_pod_subnet_cidrs,
            description="Allow k8s cluster ipblocks to talk to DB",
            from_port=DEFAULT_POSTGRES_PORT,
            protocol="tcp",
            security_groups=[],
            to_port=DEFAULT_POSTGRES_PORT,
        ),
    ],
    tags=aws_config.tags,
    vpc_id=vpc_id,
)

#########################
#     Database Setup    #
#########################
rds_defaults = defaults(stack_info)["rds"]
rds_defaults["instance_size"] = (
    airbyte_config.get("db_instance_size") or rds_defaults["instance_size"]
)
rds_defaults["use_blue_green"] = False
rds_password = airbyte_config.require("rds_password")

airbyte_db_config = OLPostgresDBConfig(
    instance_name=f"airbyte-db-{stack_info.env_suffix}",
    password=rds_password,
    storage=airbyte_config.get("db_capacity") or str(AWS_RDS_DEFAULT_DATABASE_CAPACITY),
    subnet_group_name=target_vpc["rds_subnet"],
    security_groups=[airbyte_db_security_group],
    parameter_overrides=[{"name": "rds.force_ssl", "value": 0}],
    engine_major_version="16",
    tags=aws_config.tags,
    db_name="airbyte",
    **rds_defaults,
)
airbyte_db = OLAmazonDB(airbyte_db_config)

# Shorten a few frequently used attributes from the database
db_address = airbyte_db.db_instance.address
db_port = airbyte_db.db_instance.port
db_name = airbyte_db.db_instance.db_name

# Need special database credentialsA
airbyte_role_statements = postgres_role_statements.copy()
airbyte_role_statements["app"]["create"].append(
    Template("ALTER ROLE ${app_name} WITH CREATEDB;")
)

airbyte_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=airbyte_db_config.db_name,
    mount_point=f"{airbyte_db_config.engine}-airbyte",
    db_admin_username=airbyte_db_config.username,
    db_admin_password=rds_password,
    db_host=airbyte_db.db_instance.address,
    role_statements=airbyte_role_statements,
)
airbyte_db_vault_backend = OLVaultDatabaseBackend(airbyte_db_vault_backend_config)

##################################
#     Consul Configs             #
##################################
airbyte_db_consul_node = Node(
    "airbyte-instance-db-node",
    name="airbyte-postgres-db",
    address=db_address,
    opts=consul_provider,
)

airbyte_db_consul_service = Service(
    "airbyte-instance-db-service",
    node=airbyte_db_consul_node.name,
    name="airbyte-postgres",
    port=db_port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="airbyte-instance-db",
            interval="10s",
            name="airbyte-instance-db",
            timeout="60s",
            status="passing",
            tcp=Output.all(
                address=db_address,
                port=db_port,
            ).apply(lambda db: "{address}:{port}".format(**db)),
        )
    ],
    opts=consul_provider,
)

connection_string = Output.all(address=db_address, port=db_port, name=db_name).apply(
    lambda db: (
        "jdbc:postgresql://{address}:{port}/{name}?ssl=true&sslmode=require".format(
            **db
        )
    )
)

consul.Keys(
    "airbyte-consul-template-data",
    keys=[
        consul.KeysKeyArgs(path="airbyte/database-host", value=db_address),
        consul.KeysKeyArgs(path="airbyte/database-port", value=db_port),
        consul.KeysKeyArgs(path="airbyte/database-name", value=db_name),
        consul.KeysKeyArgs(
            path="airbyte/database-connection-string",
            value=connection_string,
        ),
        consul.KeysKeyArgs(
            path="airbyte/vault-address",
            value=f"{Config('vault').get('address')}/",
        ),
        consul.KeysKeyArgs(
            path="airbyte/airbyte-hostname",
            value=airbyte_config.require("web_host_domain"),
        ),
        consul.KeysKeyArgs(
            path="airbyte/traefik-certificate-resolver",
            value=(
                "letsencrypt_staging_resolver"
                if stack_info.env_suffix != "production"
                else "letsencrypt_resolver"
            ),
        ),
        consul.KeysKeyArgs(path="airbyte/env-stage", value=stack_info.env_suffix),
    ],
    opts=consul_provider,
)

##################################
#     General K8S + IAM Config   #
##################################
airbyte_service_account_name = "airbyte-admin"

airbyte_trust_role_config = OLEKSTrustRoleConfig(
    account_id=aws_account.account_id,
    cluster_name=f"data-{stack_info.name}",
    cluster_identities=cluster_stack.require_output("cluster_identities"),
    description="Trust role for allowing the airbyte service account to "
    "access the aws API",
    policy_operator="StringEquals",
    role_name="airbyte",
    service_account_identifier=f"system:serviceaccount:{airbyte_namespace}:{airbyte_service_account_name}",
    tags=aws_config.tags,
)

airbyte_trust_role = OLEKSTrustRole(
    f"{env_name}-ol-trust-role",
    role_config=airbyte_trust_role_config,
)
iam.RolePolicyAttachment(
    f"airbyte-service-account-data-lake-access-policy-{env_name}",
    policy_arn=data_lake_policy.arn,
    role=airbyte_trust_role.role.name,
)
iam.RolePolicyAttachment(
    f"airbyte-service-account-s3-source-access-policy-{env_name}",
    policy_arn=s3_source_policy.arn,
    role=airbyte_trust_role.role.name,
)
iam.RolePolicyAttachment(
    "airbyte-service-account-policy-attachement",
    policy_arn=airbyte_app_policy.arn,
    role=airbyte_trust_role.role.name,
)

vault_k8s_resources_config = OLVaultK8SResourcesConfig(
    application_name="airbyte",
    namespace=airbyte_namespace,
    labels=k8s_global_labels,
    vault_address=vault_config.require("address"),
    vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
    vault_auth_role_name=airbyte_vault_k8s_auth_backend_role.role_name,
)
vault_k8s_resources = OLVaultK8SResources(
    resource_config=vault_k8s_resources_config,
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[airbyte_vault_k8s_auth_backend_role],
    ),
)

app_db_creds_secret_name = "app-db-creds"  # noqa: S105  # pragma: allowlist secret
app_db_creds_dynamic_secret_config = OLVaultK8SDynamicSecretConfig(
    name="airbyte-app-db-creds",
    dest_secret_labels=k8s_global_labels,
    dest_secret_name=app_db_creds_secret_name,
    exclude_raw=True,
    labels=k8s_global_labels,
    mount=airbyte_db_vault_backend_config.mount_point,
    namespace=airbyte_namespace,
    path="creds/app",
    templates={
        "DATABASE_USER": '{{ get .Secrets "username" }}',
        "DATABASE_PASSWORD": '{{  get .Secrets "password" }}',
    },
    vaultauth=vault_k8s_resources.auth_name,
)
app_db_creds_dynamic_secret = OLVaultK8SSecret(
    "airbyte-app-db-creds-vaultdynamicsecret",
    resource_config=app_db_creds_dynamic_secret_config,
)

default_resources_definition = {
    "requests": {
        "cpu": "200m",
        "memory": "0.5Gi",
    },
}

airbyte_helm_release = kubernetes.helm.v3.Release(
    "airbyte-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="airbyte",
        chart="airbyte",
        version=VERSIONS["AIRBYTE_CHART"],
        namespace=airbyte_namespace,
        cleanup_on_fail=True,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://airbytehq.github.io/charts",
        ),
        values={
            "version": AIRBYTE_VERSION,
            "global": {
                "image": {
                    "registry": "610119931565.dkr.ecr.us-east-1.amazonaws.com/dockerhub",  # noqa: E501
                    "tag": AIRBYTE_VERSION,
                },
                "airbyteUrl": f"https://{airbyte_config.require('web_host_domain')}",
                "serviceAccountName": airbyte_service_account_name,
                "deploymentMode": "oss",
                "edition": "community",
                "database": {
                    "type": "external",
                    "secretName": app_db_creds_secret_name,
                    "userSecretKey": "DATABASE_USER",  # pragma: allowlist secret
                    "passwordSecretKey": "DATABASE_PASSWORD",  # pragma: allowlist secret  # noqa: E501
                    "host": db_address,
                    "name": db_name,
                    "port": DEFAULT_POSTGRES_PORT,
                    "jdbcUrl": connection_string,
                },
                "storage": {
                    "type": "s3",
                    "bucket": {
                        "log": airbyte_bucket_name,
                        "state": airbyte_bucket_name,
                        "workloadOutput": airbyte_bucket_name,
                    },
                    "s3": {
                        "region": aws_config.region,
                        "authenticationType": "instanceProfile",
                    },
                },
                "secretsManager": {
                    "enabled": True,
                    "type": "AWS_SECRET_MANAGER",
                    "awsSecretManager": {
                        "region": "us-east-1",
                        "authenticationType": "instanceProfile",
                        "tags": [{"key": "OU", "value": "data"}],
                    },
                },
                "jobs": {
                    "resources": {
                        "limits": {"memory": "5Gi", "cpu": "1000m"},
                        "requests": {"memory": "5Gi", "cpu": "1000m"},
                    }
                },
            },
            "postgresql": {"enabled": False},
            "serviceAccount": {
                "create": True,
                "name": airbyte_service_account_name,
                "annotations": {
                    "eks.amazonaws.com/role-arn": airbyte_trust_role.role.arn.apply(
                        lambda arn: f"{arn}"
                    ),
                },
            },
            "webapp": {
                "enabled": False,
            },
            "server": {
                "enabled": True,
                "replicaCount": 2,
                "deploymentStrategyType": "RollingUpdate",
                "podLabels": k8s_global_labels,
                "resources": default_resources_definition,
                "httpIdleTimeout": "20m",
                "extraEnv": [  # How long to attempt new source schema discovery
                    {"name": "READ_TIMEOUT", "value": "30m"},
                ],
            },
            "worker": {
                "enabled": True,
                "replicaCount": 2,
                "podLabels": k8s_global_labels,
                "resources": default_resources_definition,
            },
            "workloadLauncher": {
                "enabled": True,
                "replicaCount": 1,
                "podLabels": k8s_global_labels,
                "resources": default_resources_definition,
            },
            "metrics": {
                "enabled": False,
            },
            "airbyteBootloader": {
                "enabled": True,
                "podLabels": k8s_global_labels,
                "resources": default_resources_definition,
                "log": {
                    "level": "DEBUG",
                },
            },
            "temporal": {
                "enabled": True,
                "replicaCount": 1,
                "podLabels": k8s_global_labels,
                "resources": default_resources_definition,
            },
            "cron": {
                "enabled": True,
                "replicaCount": 1,
                "podLabels": k8s_global_labels,
                "resources": default_resources_definition,
            },
        },
        skip_await=True,
    ),
    opts=ResourceOptions(
        depends_on=[
            app_db_creds_dynamic_secret,
            airbyte_trust_role,
            airbyte_db_vault_backend,
            airbyte_db,
        ],
        delete_before_replace=True,
    ),
)

# A filthy hack to override the development.yaml file that comes standard with
# the installation of temporal.
#
# This WILL NOT take effect until temporal is restarted manually with something like:
# `kubectl rollout restart deployment airbyte-temporal -n airbyte`
override_dynamicconfig_data = (
    Path(__file__).parent.joinpath("files/override-dynamicconfig.yaml").read_text()
)

override_dynamicconfig_configmap_patch = kubernetes.core.v1.ConfigMapPatch(
    "airbyte-override-dynamicconfig-configmap-patch",
    metadata=kubernetes.meta.v1.ObjectMetaPatchArgs(
        name="airbyte-temporal-dynamicconfig",
        namespace=airbyte_namespace,
        annotations={
            "pulumi.com/patchForce": "true",
        },
    ),
    data={"development.yaml": override_dynamicconfig_data},
    opts=ResourceOptions(
        parent=airbyte_helm_release,
        depends_on=[airbyte_helm_release],
        delete_before_replace=True,
    ),
)

##################################
#     Gateway + forward Auth     #
##################################
basic_auth_middleware_name = "airbyte-basic-auth"
forward_auth_middleware_name = "airbyte-forward-auth"

gateway_config = OLEKSGatewayConfig(
    cert_issuer="letsencrypt-production",
    cert_issuer_class="cluster-issuer",
    gateway_name="airbyte",
    hostnames=[
        airbyte_config.require("web_host_domain"),
        airbyte_config.require("api_host_domain"),
    ],
    namespace=airbyte_namespace,
    listeners=[
        OLEKSGatewayListenerConfig(
            name="https-web",
            hostname=airbyte_config.require("web_host_domain"),
            port=8443,
            tls_mode="Terminate",
            certificate_secret_name="airbyte-webapp-tls",  # noqa: S106 # pragma: allowlist secret
            certificate_secret_namespace=airbyte_namespace,
        ),
        OLEKSGatewayListenerConfig(
            name="https-api",
            hostname=airbyte_config.require("api_host_domain"),
            port=8443,
            tls_mode="Terminate",
            certificate_secret_name="airbyte-api-tls",  # noqa: S106 # pragma: allowlist secret
            certificate_secret_namespace=airbyte_namespace,
        ),
    ],
    routes=[
        # Some of the info here is sourced from the helm chart
        # Calls to /v1/* get basic-auth in front of them
        OLEKSGatewayRouteConfig(
            backend_service_name="airbyte-airbyte-server-svc",
            backend_service_namespace=airbyte_namespace,
            backend_service_port=8001,
            name="airbyte-https-v1",
            listener_name="https-api",
            hostnames=[airbyte_config.require("api_host_domain")],
            port=8443,
            matches=[
                {
                    "path": {
                        "type": "PathPrefix",
                        "value": "/",
                    },
                },
            ],
            filters=[
                {
                    "type": "ExtensionRef",
                    "extensionRef": {
                        "group": "traefik.io",
                        "kind": "Middleware",
                        "name": basic_auth_middleware_name,
                    },
                },
            ],
        ),
        # All other calls get forward-auth
        OLEKSGatewayRouteConfig(
            backend_service_name="airbyte-airbyte-server-svc",
            backend_service_namespace=airbyte_namespace,
            backend_service_port=8001,
            name="airbyte-https-root",
            listener_name="https-web",
            hostnames=[airbyte_config.require("web_host_domain")],
            port=8443,
            matches=[
                {
                    "path": {
                        "type": "PathPrefix",
                        "value": "/",
                    },
                },
            ],
            filters=[
                {
                    "type": "ExtensionRef",
                    "extensionRef": {
                        "group": "traefik.io",
                        "kind": "Middleware",
                        "name": forward_auth_middleware_name,
                    },
                },
            ],
        ),
    ],
)

gateway = OLEKSGateway(
    "airbyte-gateway",
    gateway_config=gateway_config,
    opts=ResourceOptions(
        parent=airbyte_helm_release,
        depends_on=[airbyte_helm_release],
        delete_before_replace=True,
    ),
)
# This basic auth is used by dagster to access the config api
basic_auth_secret_name = "airbyte-basic-auth"  # noqa: S105  # pragma: allowlist secret

basic_auth_middleware = kubernetes.apiextensions.CustomResource(
    "airbyte-basic-auth-traefik-middleware",
    api_version="traefik.io/v1alpha1",
    kind="Middleware",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=basic_auth_middleware_name,
        namespace=airbyte_namespace,
        labels=k8s_global_labels,
    ),
    spec={
        "basicAuth": {
            "secret": basic_auth_secret_name,
        },
    },
    opts=ResourceOptions(
        parent=gateway,
        depends_on=[airbyte_helm_release],
        delete_before_replace=True,
    ),
)
basic_auth_secret_config = OLVaultK8SStaticSecretConfig(
    name="airbyte-basic-auth-config",
    namespace=airbyte_namespace,
    labels=k8s_global_labels,
    dest_secret_name=basic_auth_secret_name,
    dest_secret_labels=k8s_global_labels,
    mount="secret-airbyte",
    mount_type="kv-v2",
    path="dagster",
    templates={"users": 'dagster:{{ get .Secrets "credentials" }}'},
    vaultauth=vault_k8s_resources.auth_name,
)
basic_auth_secret = OLVaultK8SSecret(
    name="airbyte-basic-auth",
    resource_config=basic_auth_secret_config,
    opts=ResourceOptions(
        parent=gateway,
        depends_on=[airbyte_helm_release],
        delete_before_replace=True,
    ),
)

airbyte_forward_auth_service_name = "airbyte-forward-auth"
airbyte_forward_auth_deployment_name = "airbyte-forward-auth"
oidc_config_secret_name = "oidc-config"  # noqa: S105  # pragma: allowlist secret

forward_auth_middleware = kubernetes.apiextensions.CustomResource(
    "airbyte-forward-auth-traefik-middleware",
    api_version="traefik.io/v1alpha1",
    kind="Middleware",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=forward_auth_middleware_name,
        namespace=airbyte_namespace,
        labels=k8s_global_labels,
    ),
    spec={
        # a lot of guides and tutorials only use the service name,
        # omitting the rest of the k8s domain. That works fine
        # if you're doing *everything* in the default namespace.
        # We are not, so we need to use the whole thing.
        "forwardAuth": {
            "address": f"http://{airbyte_forward_auth_service_name}.{airbyte_namespace}.svc.cluster.local:4181",
            "authResponseHeaders": ["X-Forwarded-User"],
        },
    },
    opts=ResourceOptions(
        parent=gateway,
        depends_on=[airbyte_helm_release],
        delete_before_replace=True,
    ),
)

forward_auth_secret_config = OLVaultK8SStaticSecretConfig(
    name="airbyte-forward-auth-oidc-config",
    namespace=airbyte_namespace,
    labels=k8s_global_labels,
    dest_secret_name=oidc_config_secret_name,
    dest_secret_labels=k8s_global_labels,
    mount="secret-operations",
    mount_type="kv-v1",
    path="sso/airbyte",
    restart_target_kind="Deployment",
    restart_target_name=airbyte_forward_auth_deployment_name,
    templates={
        "PROVIDERS_OIDC_ISSUER_URL": '{{ get .Secrets "url" }}',
        "PROVIDERS_OIDC_CLIENT_ID": '{{ get .Secrets "client_id" }}',
        "PROVIDERS_OIDC_CLIENT_SECRET": '{{ get .Secrets "client_secret" }}',
        "SECRET": '{{ get .Secrets "secret" }}',
    },
    vaultauth=vault_k8s_resources.auth_name,
)
forward_auth_secret = OLVaultK8SSecret(
    name="airbyte-forward-auth-oidc",
    resource_config=forward_auth_secret_config,
    opts=ResourceOptions(
        parent=gateway,
        depends_on=[airbyte_helm_release],
        delete_before_replace=True,
    ),
)

forward_auth_pod_labels = {
    "app.kubernetes.io/instance": "airbyte",
    "app.kubernetes.io/name": "forward-auth",
}
forward_auth_deployment = kubernetes.apps.v1.Deployment(
    airbyte_forward_auth_deployment_name,
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=airbyte_forward_auth_deployment_name,
        namespace=airbyte_namespace,
        labels=k8s_global_labels,
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        replicas=1,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels=forward_auth_pod_labels,
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels=forward_auth_pod_labels,
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        image="thomseddon/traefik-forward-auth:2",
                        name=airbyte_forward_auth_deployment_name,
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(
                                container_port=4181,
                            )
                        ],
                        env=[
                            kubernetes.core.v1.EnvVarArgs(
                                name="DEFAULT_PROVIDER",
                                value="oidc",
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="LOG_LEVEL",
                                value=airbyte_config.get("forward_auth_log_level")
                                or "info",
                            ),
                        ],
                        env_from=[
                            kubernetes.core.v1.EnvFromSourceArgs(
                                secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                                    name=oidc_config_secret_name,
                                    optional=False,
                                ),
                            )
                        ],
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            limits={
                                "memory": "128Mi",
                                "cpu": "100m",
                            },
                            requests={
                                "memory": "128Mi",
                                "cpu": "100m",
                            },
                        ),
                    )
                ],
            ),
        ),
    ),
    opts=ResourceOptions(
        parent=gateway,
        depends_on=[airbyte_helm_release],
        delete_before_replace=True,
    ),
)

forward_auth_service = kubernetes.core.v1.Service(
    airbyte_forward_auth_service_name,
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=airbyte_forward_auth_service_name,
        namespace=airbyte_namespace,
        labels=k8s_global_labels,
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        internal_traffic_policy="Cluster",
        selector=forward_auth_pod_labels,
        type=kubernetes.core.v1.ServiceSpecType.CLUSTER_IP,
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                name="forward-auth",
                port=4181,
                protocol="TCP",
                target_port=4181,
            )
        ],
    ),
    opts=ResourceOptions(
        parent=forward_auth_deployment,
        depends_on=[airbyte_helm_release, forward_auth_deployment],
        delete_before_replace=True,
    ),
)

export("lakeformation_role_arn", airbyte_lakeformation_role.arn)
