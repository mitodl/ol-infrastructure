# ruff: noqa: E501

"""Provision and deploy the resources needed for an edxapp installation.

- Create S3 buckets required by edxapp
- Create IAM role to allow access to AWS resources from edxapp instances
- Create MariaDB instance in RDS
- Create Redis cluster in Elasticache for use as a Django cache and Celery queue
- Create ALB with listeners for routing to deployed edxapp instances
- Create autoscale groups for web and worker instances
"""

import base64
import json
import textwrap
from pathlib import Path
from string import Template
from typing import cast

import pulumi_fastly as fastly
import pulumi_mongodbatlas as atlas
import pulumi_vault as vault
from pulumi import (
    ROOT_STACK_RESOURCE,
    Alias,
    Config,
    Output,
    ResourceOptions,
    StackReference,
    export,
)
from pulumi.invoke import InvokeOptions
from pulumi_aws import (
    ec2,
    get_caller_identity,
    iam,
    route53,
    s3,
    ses,
)

from bridge.lib.magic_numbers import (
    DEFAULT_HTTPS_PORT,
    DEFAULT_MYSQL_PORT,
    DEFAULT_REDIS_PORT,
    ONE_MEGABYTE_BYTE,
)
from bridge.secrets.sops import read_yaml_secrets
from bridge.settings.openedx.version_matrix import OpenLearningOpenEdxDeployment
from ol_infrastructure.applications.edxapp.k8s_resources import create_k8s_resources
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.database import OLAmazonDB, OLMariaDBConfig
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultMysqlDatabaseConfig,
)
from ol_infrastructure.lib.aws.eks_helper import setup_k8s_provider
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.aws.route53_helper import (
    fastly_certificate_validation_records,
)
from ol_infrastructure.lib.fastly import (
    build_fastly_log_format_string,
    get_fastly_provider,
)
from ol_infrastructure.lib.ol_types import AWSBase, Services
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import mysql_role_statements, setup_vault_provider

stack_info = parse_stack()

edxapp_config = Config("edxapp")
if Config("vault").get("address"):
    setup_vault_provider()

#############
# Constants #
#############
FIVE_MINUTES = 60 * 5

#####################
# Stack Information #
#####################
cluster_stack_name = (
    edxapp_config.get("cluster_stack")
    or f"infrastructure.aws.eks.applications.{stack_info.name}"
)
cluster_stack = StackReference(cluster_stack_name)
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")

kms_stack = StackReference(f"infrastructure.aws.kms.{stack_info.name}")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
monitoring_stack = StackReference("infrastructure.monitoring")
vector_log_proxy_stack = StackReference(
    f"infrastructure.vector_log_proxy.operations.{stack_info.name}"
)
mongodb_atlas_stack = StackReference(
    f"infrastructure.mongodb_atlas.{stack_info.env_prefix}.{stack_info.name}"
)
notes_stack = StackReference(
    f"applications.edxnotes.{stack_info.env_prefix}.{stack_info.name}"
)

#############
# Variables #
#############
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
target_vpc = edxapp_config.get("target_vpc") or f"{stack_info.env_prefix}_vpc"
k8s_vpc = edxapp_config.require("k8s_vpc")
aws_account = get_caller_identity()
aws_config = AWSBase(
    tags={
        "OU": edxapp_config.require("business_unit"),
        "Environment": env_name,
        "Application": Services.edxapp,
        "Owner": "platform-engineering",
    }
)

fastly_provider = get_fastly_provider()
openedx_release = (
    OpenLearningOpenEdxDeployment.get_item(stack_info.env_prefix)
    .release_by_env(stack_info.name)
    .value
)
edxapp_domains = edxapp_config.require_object("domains")
edxapp_mfes = edxapp_config.require_object("enabled_mfes")
edxapp_mfe_paths = list(edxapp_mfes.values())
edxapp_mail_domain = edxapp_config.require("mail_domain")
edxapp_vpc = network_stack.require_output(target_vpc)
edxapp_vpc_id = edxapp_vpc["id"]
k8s_vpc = network_stack.require_output(k8s_vpc)
k8s_pod_subnet_cidrs = k8s_vpc["k8s_pod_subnet_cidrs"]

data_vpc = network_stack.require_output("data_vpc")
data_integrator_secgroup = data_vpc["security_groups"]["integrator"]

edxapp_zone = dns_stack.require_output(edxapp_config.require("dns_zone"))
edxapp_zone_id = edxapp_zone["id"]
kms_ebs = kms_stack.require_output("kms_ec2_ebs_key")
kms_s3_key = kms_stack.require_output("kms_s3_data_analytics_key")
operations_vpc = network_stack.require_output("operations_vpc")
mongodb_cluster_uri = mongodb_atlas_stack.require_output("atlas_cluster")[
    "connection_strings"
][0]

if edxapp_config.get_bool("move_db") or False:
    rds_subnet = k8s_vpc["rds_subnet"]
    db_secgroup_vpc_id = k8s_vpc["id"]
    cache_subnet_group = k8s_vpc["elasticache_subnet"]
else:
    rds_subnet = edxapp_vpc["rds_subnet"]
    db_secgroup_vpc_id = edxapp_vpc_id
    cache_subnet_group = edxapp_vpc["elasticache_subnet"]


##############
# S3 Buckets #
##############

# MFE Bucket - Public read access for static assets
edxapp_mfe_bucket_name = f"{env_name}-edxapp-mfe"
edxapp_mfe_bucket_config = S3BucketConfig(
    bucket_name=edxapp_mfe_bucket_name,
    versioning_enabled=False,  # Suspended
    ownership_controls="BucketOwnerPreferred",
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
    restrict_public_buckets=False,
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
        )
    ],
    tags=aws_config.tags,
    bucket_policy_document=cast(
        str,
        lint_iam_policy(
            {
                "Version": IAM_POLICY_VERSION,
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": "*"},
                        "Action": "s3:GetObject",
                        "Resource": f"arn:aws:s3:::{edxapp_mfe_bucket_name}/*",
                    }
                ],
            },
            stringify=True,
        ),
    ),
)
edxapp_mfe_bucket = OLBucket(
    "edxapp-mfe-s3-bucket",
    config=edxapp_mfe_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(name="edxapp-mfe-s3-bucket", parent=ROOT_STACK_RESOURCE),
            Alias(
                name="edxapp-mfe-ownership-controls",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="edxapp-mfe-bucket-versioning",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="edxapp-mfe-bucket-public-access-controls",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="edxapp-mfe-bucket-policy",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="edxapp-mfe-bucket-cors-rules",
                parent=ROOT_STACK_RESOURCE,
            ),
        ]
    ),
)


# Storage Bucket - Public read access for video images
storage_bucket_name = f"{env_name}-edxapp-storage"
edxapp_storage_bucket_config = S3BucketConfig(
    bucket_name=storage_bucket_name,
    versioning_enabled=True,
    ownership_controls="BucketOwnerPreferred",
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
    restrict_public_buckets=False,
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_headers=["*"],
            allowed_methods=["GET", "PUT", "POST", "HEAD"],
            allowed_origins=[f"https://{domain}" for domain in edxapp_domains.values()],
            expose_headers=["ETag"],
            max_age_seconds=3000,
        )
    ],
    tags=aws_config.tags,
    bucket_policy_document=cast(
        str,
        lint_iam_policy(
            {
                "Version": IAM_POLICY_VERSION,
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": "*",
                        "Action": "s3:GetObject",
                        "Resource": (
                            f"arn:aws:s3:::{storage_bucket_name}/media/video-images/*"
                        ),
                    }
                ],
            },
            stringify=True,
        ),
    ),
)
edxapp_storage_bucket = OLBucket(
    "edxapp-storage-s3-bucket",
    config=edxapp_storage_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(name="edxapp-storage-s3-bucket", parent=ROOT_STACK_RESOURCE),
            Alias(
                name="edxapp-storage-ownership-controls",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="edxapp-storage-bucket-versioning",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="edxapp-storage-bucket-public-access-controls",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="edxapp-storage-bucket-policy",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="edxapp-storage-bucket-cors-rules",
                parent=ROOT_STACK_RESOURCE,
            ),
        ]
    ),
)

# Courses Bucket - Private
course_bucket_name = f"{env_name}-edxapp-courses"
edxapp_course_bucket_config = S3BucketConfig(
    bucket_name=course_bucket_name,
    versioning_enabled=False,  # Suspended
    tags=aws_config.tags,
)
edxapp_course_bucket = OLBucket(
    "edxapp-courses-s3-bucket",
    config=edxapp_course_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(name="edxapp-courses-s3-bucket", parent=ROOT_STACK_RESOURCE),
            Alias(
                name="edxapp-course-bucket-versioning",
                parent=ROOT_STACK_RESOURCE,
            ),
        ]
    ),
)

# Grades Bucket - Private
grades_bucket_name = f"{env_name}-edxapp-grades"
edxapp_grades_bucket_config = S3BucketConfig(
    bucket_name=grades_bucket_name,
    versioning_enabled=True,
    tags=aws_config.tags,
)
edxapp_grades_bucket = OLBucket(
    "edxapp-grades-s3-bucket",
    config=edxapp_grades_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(name="edxapp-grades-s3-bucket", parent=ROOT_STACK_RESOURCE),
            Alias(
                name="edxapp-grades-bucket-versioning",
                parent=ROOT_STACK_RESOURCE,
            ),
        ]
    ),
)


# Tracking Logs Bucket - Private with KMS encryption
tracking_bucket_name = f"{env_name}-edxapp-tracking"
edxapp_tracking_bucket_config = S3BucketConfig(
    bucket_name=tracking_bucket_name,
    versioning_enabled=True,
    ownership_controls="BucketOwnerPreferred",
    server_side_encryption_enabled=True,
    kms_key_id=kms_s3_key["id"],
    bucket_key_enabled=True,
    tags=aws_config.tags,
)
edxapp_tracking_bucket = OLBucket(
    "edxapp-tracking-logs-s3-bucket",
    config=edxapp_tracking_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(name="edxapp-tracking-logs-s3-bucket", parent=ROOT_STACK_RESOURCE),
            Alias(
                name="edxapp-tracking-logs-bucket-ownership-controls",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="edxapp-tracking-logs-bucket-public-access-controls",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="edxapp-tracking-logs-s3-bucket-encryption",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="edxapp-tracking-logs-bucket-versioning",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="edxapp-tracking-bucket-prevent-public-access",
                parent=ROOT_STACK_RESOURCE,
            ),
        ]
    ),
)

########################
# IAM Roles & Policies #
########################

parliament_config = {
    "PERMISSIONS_MANAGEMENT_ACTIONS": {
        "ignore_locations": [{"actions": ["s3:putobjectacl"]}]
    },
    "RESOURCE_MISMATCH": {},
}

edxapp_policy_document = {
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
                f"arn:aws:s3:::{storage_bucket_name}",
                f"arn:aws:s3:::{storage_bucket_name}/*",
                f"arn:aws:s3:::{grades_bucket_name}",
                f"arn:aws:s3:::{grades_bucket_name}/*",
                f"arn:aws:s3:::{course_bucket_name}",
                f"arn:aws:s3:::{course_bucket_name}/*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject*",
                "s3:PutObject",
                "s3:ListBucket",
            ],
            "Resource": [
                f"arn:aws:s3:::{tracking_bucket_name}",
                f"arn:aws:s3:::{tracking_bucket_name}/*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": ["ses:SendEmail", "ses:SendRawEmail"],
            "Resource": [
                "arn:*:ses:*:*:identity/*mit.edu",
                f"arn:aws:ses:*:*:configuration-set/edxapp-{stack_info.env_prefix}-{stack_info.env_suffix}",
            ],
        },
        {
            "Effect": "Allow",
            "Action": ["ses:GetSendQuota"],
            "Resource": "*",
        },
    ],
}

edxapp_policy = iam.Policy(
    "edxapp-policy",
    name_prefix="edxapp-policy-",
    path=f"/ol-applications/edxapp/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        edxapp_policy_document,
        stringify=True,
        parliament_config=parliament_config,
    ),
    description="AWS access permissions for edX application instances",
)

##################################
#     Network Access Control     #
##################################

# Create security group for edxapp MariaDB database
edxapp_db_security_group = ec2.SecurityGroup(
    f"edxapp-db-access-{stack_info.env_suffix}",
    name_prefix=f"edxapp-db-access-{env_name}-",
    description="Access from Edxapp instances to the associated MariaDB database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                data_vpc["security_groups"]["orchestrator"],
                data_vpc["security_groups"]["integrator"],
                vault_stack.require_output("vault_server")["security_group"],
            ],
            cidr_blocks=data_vpc["k8s_pod_subnet_cidrs"].apply(
                lambda pod_cidrs: [*pod_cidrs, edxapp_vpc["cidr"]]
            ),
            protocol="tcp",
            from_port=DEFAULT_MYSQL_PORT,
            to_port=DEFAULT_MYSQL_PORT,
            description="Access to MariaDB from Edxapp web nodes",
        ),
        # This is needed because the security group pinning near the bottom does not work
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=k8s_pod_subnet_cidrs.apply(lambda pod_cidrs: [*pod_cidrs]),
            protocol="tcp",
            from_port=DEFAULT_MYSQL_PORT,
            to_port=DEFAULT_MYSQL_PORT,
            description="Access to MariaDB from K8s application pods",
        ),
    ],
    tags=aws_config.tags,
    vpc_id=db_secgroup_vpc_id,
)

######################
# Secrets Management #
######################
edxapp_vault_mount = vault.Mount(
    "edxapp-vault-generic-secrets-mount",
    path=f"secret-{stack_info.env_prefix}",
    description=(
        "Static secrets storage for Open edX {stack_info.env_prefix} applications and"
        " services"
    ),
    type="kv",
)
edxapp_secrets = vault.generic.Secret(
    "edxapp-static-secrets",
    path=edxapp_vault_mount.path.apply("{}/edxapp".format),
    data_json=Output.secret(
        read_yaml_secrets(
            Path(f"edxapp/{stack_info.env_prefix}.{stack_info.env_suffix}.yaml")
        )
    ).apply(json.dumps),
)
forum_secrets = vault.generic.Secret(
    "edx-forum-static-secrets",
    path=edxapp_vault_mount.path.apply("{}/edx-forum".format),
    data_json=edxapp_config.require_secret_object("edx_forum_secrets").apply(
        json.dumps
    ),
)
if xqueue_secret := edxapp_config.get_secret_object("edx_xqueue_secrets"):
    xqueue_secrets = vault.generic.Secret(
        "edx-xqueue-static-secrets",
        path=edxapp_vault_mount.path.apply("{}/edx-xqueue".format),
        data_json=xqueue_secret.apply(json.dumps),
    )

# Vault policy definition
edxapp_vault_policy = vault.Policy(
    "edxapp-vault-policy",
    name=f"edxapp-{stack_info.env_prefix}",
    policy=Path(__file__)
    .parent.joinpath(f"edxapp_{stack_info.env_prefix}_policy.hcl")
    .read_text(),
)

##########################
#     Database Setup     #
##########################
rds_defaults = defaults(stack_info)["rds"]
rds_defaults["instance_size"] = (
    edxapp_config.get("db_instance_size") or rds_defaults["instance_size"]
)
edxapp_db_config = OLMariaDBConfig(
    instance_name=f"edxapp-db-{env_name}",
    password=edxapp_config.require("db_password"),
    subnet_group_name=rds_subnet,
    security_groups=[edxapp_db_security_group],
    engine_major_version=edxapp_config.get("db_version") or "11.8",
    tags=aws_config.tags,
    db_name="edxapp",
    storage=edxapp_config.get_int("db_storage_gb") or 50,
    max_storage=edxapp_config.get_int("db_max_storage_gb") or 1000,
    use_blue_green=edxapp_config.get_bool("db_use_blue_green") or False,
    **rds_defaults,
)
edxapp_db = OLAmazonDB(edxapp_db_config)

edxapp_mysql_role_statements = mysql_role_statements.copy()
edxapp_mysql_role_statements.pop("app")
edxapp_mysql_role_statements["edxapp"] = {
    "create": [
        Template("""CREATE DATABASE IF NOT EXISTS edxapp;"""),
        Template("""CREATE DATABASE IF NOT EXISTS edxapp_csmh;"""),
        Template("""CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';"""),
        Template(
            """
            GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, DROP, ALTER,
            REFERENCES, CREATE TEMPORARY TABLES, LOCK TABLES, CREATE VIEW, SHOW VIEW
            ON edxapp.* TO '{{name}}'@'%';
            """
        ),
        Template(
            """
            GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, DROP, ALTER,
            REFERENCES, CREATE TEMPORARY TABLES, LOCK TABLES, CREATE VIEW, SHOW VIEW
            ON edxapp_csmh.* TO '{{name}}'@'%';
            """
        ),
    ],
    "revoke": [Template("DROP USER '{{name}}';")],
    "renew": [],
    "rollback": [],
}
edxapp_mysql_role_statements["edxapp-csmh"] = {
    "create": [
        Template("""CREATE DATABASE IF NOT EXISTS edxapp_csmh;"""),
        Template("""CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';"""),
        Template(
            """
            GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, DROP, ALTER,
            REFERENCES, CREATE TEMPORARY TABLES, LOCK TABLES, CREATE VIEW, SHOW VIEW
            ON edxapp_csmh.* TO '{{name}}'@'%';
            """
        ),
    ],
    "revoke": [Template("DROP USER '{{name}}';")],
    "renew": [],
    "rollback": [],
}
edxapp_mysql_role_statements["xqueue"] = {
    "create": [
        Template("""CREATE DATABASE IF NOT EXISTS xqueue;"""),
        Template("""CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';"""),
        Template(
            """
            GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, DROP, ALTER, REFERENCES,
            CREATE TEMPORARY TABLES, LOCK TABLES ON xqueue.* TO '{{name}}'@'%';
            """
        ),
    ],
    "revoke": [Template("DROP USER '{{name}}';")],
    "renew": [],
    "rollback": [],
}
edxapp_mysql_role_statements["notes"] = {
    "create": [
        Template("""CREATE DATABASE IF NOT EXISTS edx_notes_api;"""),
        Template("""CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';"""),
        Template(
            """
            GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, DROP, ALTER, REFERENCES,
            CREATE TEMPORARY TABLES, LOCK TABLES ON edx_notes_api.* TO '{{name}}'@'%';
            """
        ),
    ],
    "revoke": [
        Template("DROP USER '{{name}}';"),
    ],
    "renew": [],
    "rollback": [],
}

edxapp_db_vault_backend_config = OLVaultMysqlDatabaseConfig(
    db_name=edxapp_db_config.db_name,
    mount_point=f"{edxapp_db_config.engine}-{stack_info.env_prefix}",
    db_admin_username=edxapp_db_config.username,
    db_admin_password=edxapp_config.require("db_password"),
    db_host=edxapp_db.db_instance.address,
    role_statements=edxapp_mysql_role_statements,
)
edxapp_db_vault_backend = OLVaultDatabaseBackend(edxapp_db_vault_backend_config)

#######################
# MongoDB Vault Setup #
#######################
mongodb_config = Config("mongodb")
atlas_project_id = mongodb_config.get("atlas_project_id")
atlas_creds = read_yaml_secrets(Path("pulumi/mongodb_atlas.yaml"))
atlas_provider = ResourceOptions(
    provider=atlas.Provider(
        "mongodb-atlas-provider",
        private_key=atlas_creds["private_key"],
        public_key=atlas_creds["public_key"],
    )
)
mongo_atlas_credentials = read_yaml_secrets(
    Path(f"pulumi/mongodb_atlas.{stack_info.env_prefix}.{stack_info.env_suffix}.yaml")
)
edxapp_mongo_user = atlas.DatabaseUser(
    "mongodb-atlas-edxapp-user",
    project_id=atlas_project_id,
    auth_database_name="admin",
    password=Output.secret(mongo_atlas_credentials["edxapp"]),
    username="edxapp",
    roles=[atlas.DatabaseUserRoleArgs(database_name="edxapp", role_name="readWrite")],
    opts=atlas_provider.merge(ResourceOptions(delete_before_replace=True)),
)
forum_mongo_user = atlas.DatabaseUser(
    "mongodb-atlas-forum-user",
    project_id=atlas_project_id,
    auth_database_name="admin",
    password=Output.secret(mongo_atlas_credentials["forum"]),
    username="forum",
    roles=[atlas.DatabaseUserRoleArgs(database_name="forum", role_name="readWrite")],
    opts=atlas_provider.merge(ResourceOptions(delete_before_replace=True)),
)
vault.generic.Secret(
    "edxapp-mongodb-atlas-user-password",
    path=edxapp_vault_mount.path.apply("{}/mongodb-edxapp".format),
    data_json=json.dumps(
        {"username": "edxapp", "password": mongo_atlas_credentials["edxapp"]}
    ),
)
vault.generic.Secret(
    "forum-mongodb-atlas-user-password",
    path=edxapp_vault_mount.path.apply("{}/mongodb-forum".format),
    data_json=mongodb_cluster_uri.apply(
        lambda uri: json.dumps(
            {
                "username": "forum",
                "password": mongo_atlas_credentials["forum"],
                "uri": uri["standard_srv"],  # This is used by Dagster
            }
        ),
    ),
)

###########################
# Redis Elasticache Setup #
###########################

redis_config = Config("redis")
redis_cluster_security_group = ec2.SecurityGroup(
    f"edxapp-redis-cluster-{env_name}",
    name_prefix=f"edxapp-redis-{env_name}-",
    description="Grant access to Redis from Open edX",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            protocol="tcp",
            security_groups=[],
            cidr_blocks=k8s_pod_subnet_cidrs.apply(lambda pod_cidrs: pod_cidrs),
            description="Allow access from edX to Redis for caching and queueing",
        ),
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            protocol="tcp",
            cidr_blocks=operations_vpc["k8s_pod_subnet_cidrs"],
            description="Allow access from Operations VPC celery monitoring pods to Redis",
        ),
    ],
    tags=aws_config.merged_tags({"Name": f"edxapp-redis-{env_name}"}),
    vpc_id=edxapp_vpc_id,
)

redis_instance_type = (
    redis_config.get("instance_type") or defaults(stack_info)["redis"]["instance_type"]
)
redis_cache_config = OLAmazonRedisConfig(
    encrypt_transit=True,
    auth_token=read_yaml_secrets(
        Path(f"edxapp/{stack_info.env_prefix}.{stack_info.env_suffix}.yaml")
    )["redis_auth_token"],
    cluster_mode_enabled=False,
    encrypted=True,
    engine="valkey",
    engine_version="7.2",
    instance_type=redis_instance_type,
    num_instances=3,
    shard_count=1,
    auto_upgrade=True,
    cluster_description="Redis cluster for edX platform tasks and caching",
    cluster_name=f"edxapp-redis-{env_name}",
    parameter_overrides={"maxmemory-policy": "allkeys-lru"},
    security_groups=[redis_cluster_security_group.id],
    subnet_group=edxapp_vpc[
        "elasticache_subnet"
    ],  # the name of the subnet group created in the OLVPC component resource
    tags=aws_config.tags,
)
edxapp_redis_cache = OLAmazonCache(
    redis_cache_config,
    opts=ResourceOptions(
        aliases=[Alias(name=f"edxapp-redis-{env_name}-redis-elasticache-cluster")]
    ),
)

########################################
# Create SES Service For edxapp Emails #
########################################

edxapp_ses_domain_identity = ses.DomainIdentity(
    "edxapp-ses-domain-identity",
    domain=edxapp_mail_domain,
)
edxapp_ses_verification_record = route53.Record(
    "edxapp-ses-domain-identity-verification-dns-record",
    zone_id=edxapp_zone_id,
    name=edxapp_ses_domain_identity.id.apply("_amazonses.{}".format),
    type="TXT",
    allow_overwrite=True,
    ttl=FIVE_MINUTES,
    records=[edxapp_ses_domain_identity.verification_token],
)
edxapp_ses_domain_identity_verification = ses.DomainIdentityVerification(
    "edxapp-ses-domain-identity-verification-resource",
    domain=edxapp_ses_domain_identity.id,
    opts=ResourceOptions(depends_on=[edxapp_ses_verification_record]),
)
edxapp_mail_from_domain = ses.MailFrom(
    "edxapp-ses-mail-from-domain",
    domain=edxapp_ses_domain_identity_verification.domain,
    mail_from_domain=edxapp_ses_domain_identity_verification.domain.apply(
        "bounce.{}".format
    ),
)
edxapp_mail_from_address = ses.EmailIdentity(
    "edxapp-ses-mail-from-identity",
    email=edxapp_config.require("sender_email_address"),
)
# Example Route53 MX record
edxapp_ses_domain_mail_from_mx = route53.Record(
    f"edxapp-ses-mail-from-mx-record-for-{env_name}",
    zone_id=edxapp_zone_id,
    name=edxapp_mail_from_domain.mail_from_domain,
    type="MX",
    allow_overwrite=True,
    ttl=FIVE_MINUTES,
    records=["10 feedback-smtp.us-east-1.amazonses.com"],
)
ses_domain_mail_from_txt = route53.Record(
    "edxapp-ses-domain-mail-from-text-record",
    zone_id=edxapp_zone_id,
    name=edxapp_mail_from_domain.mail_from_domain,
    type="TXT",
    allow_overwrite=True,
    ttl=FIVE_MINUTES,
    records=["v=spf1 include:amazonses.com -all"],
)
edxapp_ses_domain_dkim = ses.DomainDkim(
    "edxapp-ses-domain-dkim", domain=edxapp_ses_domain_identity.domain
)
for loop_counter in range(3):
    route53.Record(
        f"edxapp-ses-domain-dkim-record-{loop_counter}",
        zone_id=edxapp_zone_id,
        name=edxapp_ses_domain_dkim.dkim_tokens[loop_counter].apply(
            lambda dkim_name: f"{dkim_name}._domainkey.{edxapp_mail_domain}"
        ),
        type="CNAME",
        ttl=FIVE_MINUTES,
        allow_overwrite=True,
        records=[
            edxapp_ses_domain_dkim.dkim_tokens[loop_counter].apply(
                "{}.dkim.amazonses.com".format
            )
        ],
    )
edxapp_ses_configuration_set = ses.ConfigurationSet(
    "edxapp-ses-configuration-set",
    reputation_metrics_enabled=True,
    sending_enabled=True,
    name=f"edxapp-{env_name}",
)
edxapp_ses_event_destintations = ses.EventDestination(
    "edxapp-ses-event-destination-routing",
    configuration_set_name=edxapp_ses_configuration_set.name,
    enabled=True,
    matching_types=[
        "send",
        "reject",
        "bounce",
        "complaint",
        "delivery",
        "open",
        "click",
        "renderingFailure",
    ],
    cloudwatch_destinations=[
        ses.EventDestinationCloudwatchDestinationArgs(
            default_value=f"edxapp-{stack_info.env_prefix}",
            dimension_name=f"edxapp-{stack_info.env_prefix}",
            value_source="messageTag",
        )
    ],
)

########################
# Fastly CDN Managment #
########################
lms_backend_address = edxapp_config.require("backend_lms_domain")
lms_backend_ssl_hostname = edxapp_config.require("backend_lms_domain")
cms_backend_address = edxapp_config.require("backend_studio_domain")
cms_backend_ssl_hostname = edxapp_config.require("backend_studio_domain")
preview_backend_address = edxapp_config.require("backend_preview_domain")
preview_backend_ssl_hostname = edxapp_config.require("backend_preview_domain")

vector_log_proxy_secrets = read_yaml_secrets(
    Path(f"vector/vector_log_proxy.{stack_info.env_suffix}.yaml")
)
fastly_proxy_credentials = vector_log_proxy_secrets["fastly"]
encoded_fastly_proxy_credentials = base64.b64encode(
    f"{fastly_proxy_credentials['username']}:{fastly_proxy_credentials['password']}".encode()
).decode("utf8")

vector_log_proxy_domain = vector_log_proxy_stack.require_output(
    "vector_log_proxy_domain"
)

fastly_access_logging_bucket = monitoring_stack.require_output(
    "fastly_access_logging_bucket"
)
fastly_access_logging_iam_role = monitoring_stack.require_output(
    "fastly_access_logging_iam_role"
)


mfe_regex = "^/({})/".format("|".join(edxapp_mfe_paths))
edxapp_fastly_service = fastly.ServiceVcl(
    f"fastly-{stack_info.env_prefix}-{stack_info.env_suffix}",
    name=f"{stack_info.env_prefix} {stack_info.env_suffix} edX",
    comment="Managed by Pulumi",
    backends=[
        fastly.ServiceVclBackendArgs(
            address=edxapp_mfe_bucket.bucket_v2.bucket_domain_name,
            name="MFE S3 Bucket",
            override_host=edxapp_mfe_bucket.bucket_v2.bucket_domain_name,
            port=DEFAULT_HTTPS_PORT,
            request_condition="MFE Path",
            ssl_cert_hostname=edxapp_mfe_bucket.bucket_v2.bucket_domain_name,
            ssl_sni_hostname=edxapp_mfe_bucket.bucket_v2.bucket_domain_name,
            use_ssl=True,
        ),
        fastly.ServiceVclBackendArgs(
            address=lms_backend_address,
            name="AWS ALB for edxapp",
            port=DEFAULT_HTTPS_PORT,
            override_host=edxapp_domains["lms"],
            ssl_cert_hostname=lms_backend_ssl_hostname,
            ssl_sni_hostname=lms_backend_ssl_hostname,
            use_ssl=True,
            # Increase the timeout to account for slow API responses
            first_byte_timeout=60000,
            between_bytes_timeout=15000,
        ),
        fastly.ServiceVclBackendArgs(
            address=cms_backend_address,
            name="AWS ALB for edX Studio",
            port=DEFAULT_HTTPS_PORT,
            override_host=edxapp_domains["studio"],
            ssl_cert_hostname=cms_backend_ssl_hostname,
            ssl_sni_hostname=cms_backend_ssl_hostname,
            use_ssl=True,
            request_condition="studio host",
            # Increase the timeout to account for slow API responses
            first_byte_timeout=600000,
            between_bytes_timeout=15000,
        ),
    ],
    cache_settings=[
        fastly.ServiceVclCacheSettingArgs(
            action="pass",
            cache_condition="Django Admin Route",
            name="Django Admin Route",
        )
    ],
    conditions=[
        fastly.ServiceVclConditionArgs(
            name="studio host",
            statement=f'req.http.host == "{edxapp_domains["studio"]}" && req.url.path !~ "{mfe_regex}"',
            type="REQUEST",
        ),
        fastly.ServiceVclConditionArgs(
            name="Django Admin Route",
            statement='req.url ~ "^/admin"',
            type="CACHE",
        ),
        fastly.ServiceVclConditionArgs(
            name="MFE Path",
            statement=f'req.url.path ~ "{mfe_regex}"',
            type="REQUEST",
        ),
    ],
    dictionaries=[
        fastly.ServiceVclDictionaryArgs(
            name="marketing_redirects",
        )
    ],
    domains=[
        fastly.ServiceVclDomainArgs(
            comment=f"{stack_info.env_prefix} {stack_info.env_suffix} edX Application",
            name=edxapp_domains["lms"],
        ),
        fastly.ServiceVclDomainArgs(
            comment=f"{stack_info.env_prefix} {stack_info.env_suffix} edX Studio",
            name=edxapp_domains["studio"],
        ),
    ],
    headers=[
        fastly.ServiceVclHeaderArgs(
            action="set",
            destination="http.Strict-Transport-Security",
            name="Generated by force TLS and enable HSTS",
            source='"max-age=300"',
            type="response",
        )
    ],
    request_settings=[
        fastly.ServiceVclRequestSettingArgs(
            force_ssl=True,
            name="Generated by force TLS and enable HSTS",
            xff="",
        )
    ],
    snippets=[
        fastly.ServiceVclSnippetArgs(
            content=textwrap.dedent(
                """\
                if (table.contains(marketing_redirects, req.url.path)) {
                  error 618 "redirect";
                }"""
            ),
            name="Interrupt Redirected Requests",
            type="recv",
        ),
        fastly.ServiceVclSnippetArgs(
            content=textwrap.dedent(
                f"""\
                if (req.url.path ~ "{mfe_regex}") {{
                  set req.url = req.url.path;
                  unset req.http.Cookie;
                }}"""
            ),
            name="Strip headers to S3 backend",
            type="recv",
        ),
        fastly.ServiceVclSnippetArgs(
            content=textwrap.dedent(
                """\
                if (req.url.path ~ "^/asset-v1:") {
                  set beresp.ttl = 30s;
                  return (deliver);
                }
                """
            ),
            name="Shorten the TTL for assets uploaded in studio",
            type="fetch",
        ),
        fastly.ServiceVclSnippetArgs(
            content=textwrap.dedent(
                f"""\
                if (beresp.status == 404 && req.url.path ~ "{mfe_regex}") {{
                  error 600 "### Custom Response";
                }}"""
            ),
            name="Manage 404 On S3 Origin for MFE",
            type="fetch",
        ),
        fastly.ServiceVclSnippetArgs(
            content=textwrap.dedent(
                f"""\
                declare local var.mfe_path STRING;
                if (obj.status == 600) {{
                  set var.mfe_path = regsub(req.url.path, "{mfe_regex}.*", "\\1");
                  set req.url = "/" + var.mfe_path + "/index.html";
                  restart;
                }}"""
            ),
            name="Fetch site index for MFE custom error",
            priority=120,
            type="error",
        ),
        fastly.ServiceVclSnippetArgs(
            content=textwrap.dedent(
                """\
                if (obj.status == 618 && obj.response == "redirect") {
                  set obj.status = 302;
                  set obj.http.Location = table.lookup(marketing_redirects, req.url.path) + if (req.url.qs, "?" req.url.qs, "");
                  return (deliver);
                }"""
            ),
            name="Route Redirect Requests",
            type="error",
        ),
    ],
    logging_https=[
        fastly.ServiceVclLoggingHttpArgs(
            url=Output.all(domain=vector_log_proxy_domain).apply(
                lambda kwargs: f"https://{kwargs['domain']}/fastly"
            ),
            name=f"fastly-{env_name}-https-logging-args",
            content_type="application/json",
            format=build_fastly_log_format_string(
                additional_static_fields={
                    "application": "edxapp",
                    "environment": env_name,
                }
            ),
            format_version=2,
            header_name="Authorization",
            header_value=f"Basic {encoded_fastly_proxy_credentials}",
            json_format="0",
            method="POST",
            request_max_bytes=ONE_MEGABYTE_BYTE,
        )
    ],
    logging_s3s=[
        fastly.ServiceVclLoggingS3Args(
            bucket_name=fastly_access_logging_bucket["bucket_name"],
            name=f"fastly-{env_name}-s3-logging-args",
            format=build_fastly_log_format_string(additional_static_fields={}),
            gzip_level=3,
            message_type="blank",
            path=f"/edxapp/{stack_info.env_prefix}/{stack_info.env_suffix}/",
            redundancy="standard",
            s3_iam_role=fastly_access_logging_iam_role["role_arn"],
        ),
    ],
    opts=fastly_provider,
)

tls_configuration = fastly.get_tls_configuration(
    default=False,
    name="TLS v1.3",
    tls_protocols=["1.2", "1.3"],
    opts=InvokeOptions(provider=fastly_provider.provider),
)

edxapp_fastly_tls = fastly.TlsSubscription(
    f"fastly-{stack_info.env_prefix}-{stack_info.env_suffix}-tls-subscription",
    # valid values are certainly, lets-encrypt, or globalsign
    certificate_authority="certainly",
    domains=edxapp_fastly_service.domains.apply(
        lambda domains: [domain.name for domain in domains]
    ),
    # Retrieved from 0https://manage.fastly.com/network/tls-configurations
    configuration_id=tls_configuration.id,
    opts=fastly_provider,
)

edxapp_fastly_tls.managed_dns_challenges.apply(fastly_certificate_validation_records)

validated_tls_subscription = fastly.TlsSubscriptionValidation(
    "ol-redirect-service-tls-subscription-validation",
    subscription_id=edxapp_fastly_tls.id,
    opts=fastly_provider,
)


# Create Route53 DNS records for Edxapp web nodes
for domain_key, domain_value in edxapp_domains.items():
    dns_override = edxapp_config.get("maintenance_page_dns")
    if domain_key in {"studio", "lms"}:
        route53.Record(
            f"edxapp-web-{domain_key}-dns-record",
            name=domain_value,
            type="CNAME",
            ttl=FIVE_MINUTES,
            allow_overwrite=True,
            records=[dns_override or "j.sni.global.fastly.net"],
            zone_id=edxapp_zone_id,
        )
    else:
        route53.Record(
            f"edxapp-web-{domain_key}-dns-record",
            name=domain_value,
            type="CNAME",
            ttl=FIVE_MINUTES,
            allow_overwrite=True,
            records=[dns_override or preview_backend_address],
            zone_id=edxapp_zone_id,
        )

# Actions to take when the the stack is configured to deploy into k8s
k8s_resources = create_k8s_resources(
    aws_config=aws_config,
    cluster_stack=cluster_stack,
    edxapp_cache=edxapp_redis_cache,
    edxapp_config=edxapp_config,
    edxapp_db=edxapp_db,
    edxapp_iam_policy=edxapp_policy,
    mongodb_atlas_stack=mongodb_atlas_stack,
    network_stack=network_stack,
    notes_stack=notes_stack,
    stack_info=stack_info,
    vault_config=Config("vault"),
    vault_policy=edxapp_vault_policy,
)

export_dict = {
    "mariadb": edxapp_db.db_instance.address,
    "redis": edxapp_redis_cache.address,
    "redis_token": edxapp_redis_cache.cache_cluster.auth_token,
    "mfe_bucket": edxapp_mfe_bucket_name,
    "ses_configuration_set": edxapp_ses_configuration_set.name,
    "deployment": stack_info.env_prefix,
}

export("edxapp", export_dict)

if edxapp_db_config.read_replica:
    export("edxapp_read_replica", edxapp_db.db_replica.address)
