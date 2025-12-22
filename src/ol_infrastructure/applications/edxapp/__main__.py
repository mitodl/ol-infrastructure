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
import os
import textwrap
from functools import partial
from pathlib import Path
from string import Template

import pulumi_consul as consul
import pulumi_fastly as fastly
import pulumi_mongodbatlas as atlas
import pulumi_vault as vault
import yaml
from pulumi import Alias, Config, Output, ResourceOptions, StackReference, export
from pulumi.invoke import InvokeOptions
from pulumi_aws import (
    acm,
    autoscaling,
    cloudwatch,
    ec2,
    get_caller_identity,
    iam,
    lb,
    route53,
    s3,
    ses,
)
from pulumi_consul import Node, Service, ServiceCheckArgs

from bridge.lib.magic_numbers import (
    AWS_LOAD_BALANCER_NAME_MAX_LENGTH,
    DEFAULT_HTTP_PORT,
    DEFAULT_HTTPS_PORT,
    DEFAULT_MYSQL_PORT,
    DEFAULT_REDIS_PORT,
    IAM_ROLE_NAME_PREFIX_MAX_LENGTH,
    ONE_MEGABYTE_BYTE,
)
from bridge.secrets.sops import read_yaml_secrets
from bridge.settings.openedx.version_matrix import OpenLearningOpenEdxDeployment
from ol_infrastructure.applications.edxapp.k8s_resources import create_k8s_resources
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.database import OLAmazonDB, OLMariaDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultMysqlDatabaseConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import (
    DiskTypes,
    InstanceTypes,
    default_egress_args,
)
from ol_infrastructure.lib.aws.eks_helper import setup_k8s_provider
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.aws.route53_helper import (
    acm_certificate_validation_records,
    fastly_certificate_validation_records,
)
from ol_infrastructure.lib.consul import get_consul_provider
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
SSH_ACCESS_KEY_NAME = edxapp_config.get("ssh_key_name") or "oldevops"
MIN_WEB_NODES_DEFAULT = 3
MAX_WEB_NODES_DEFAULT = 15
MIN_WORKER_NODES_DEFAULT = 1
MAX_WORKER_NODES_DEFAULT = 5
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
consul_stack = StackReference(
    f"infrastructure.consul.{stack_info.env_prefix}.{stack_info.name}"
)
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
consul_security_groups = consul_stack.require_output("security_groups")

consul_provider = get_consul_provider(stack_info)
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

framework = edxapp_config.get("framework") or "docker"

ami_filters = [
    ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
    ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ec2.GetAmiFilterArgs(name="tag:deployment", values=[stack_info.env_prefix]),
    ec2.GetAmiFilterArgs(name="tag:openedx_release", values=[openedx_release]),
    ec2.GetAmiFilterArgs(name="tag:framework", values=[framework]),
]
web_filters = [ec2.GetAmiFilterArgs(name="name", values=["edxapp-web-*"]), *ami_filters]
if web_ami_id := os.environ.get("EDXAPP_WEB_AMI_ID"):
    web_filters.append(
        ec2.GetAmiFilterArgs(name="image-id", values=[web_ami_id]),
    )
edxapp_web_ami = ec2.get_ami(
    filters=web_filters,
    most_recent=True,
    owners=[aws_account.account_id],
)
worker_filters = [
    ec2.GetAmiFilterArgs(name="name", values=["edxapp-worker-*"]),
    *ami_filters,
]
if worker_ami_id := os.environ.get("EDXAPP_WORKER_AMI_ID"):
    worker_filters.append(
        ec2.GetAmiFilterArgs(name="image-id", values=[worker_ami_id]),
    )
edxapp_worker_ami = ec2.get_ami(
    filters=worker_filters,
    most_recent=True,
    owners=[aws_account.account_id],
)

edxapp_zone = dns_stack.require_output(edxapp_config.require("dns_zone"))
edxapp_zone_id = edxapp_zone["id"]
kms_ebs = kms_stack.require_output("kms_ec2_ebs_key")
kms_s3_key = kms_stack.require_output("kms_s3_data_analytics_key")
operations_vpc = network_stack.require_output("operations_vpc")
mongodb_cluster_uri = mongodb_atlas_stack.require_output("atlas_cluster")[
    "connection_strings"
][0]


##############
# Helper for creating user_data
##############
def cloud_init_user_data_func(
    consul_env_name,
):
    grafana_credentials = read_yaml_secrets(
        Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
    )
    # Used to switch which staticfiles collection to used
    environment_tier = (
        "production" if stack_info.env_suffix == "production" else "nonprod"
    )
    cloud_config_content = {
        "write_files": [
            {
                "path": "/etc/consul.d/99-autojoin.json",
                "content": json.dumps(
                    {
                        "retry_join": [
                            "provider=aws tag_key=consul_env "
                            f"tag_value={consul_env_name}"
                        ],
                        "datacenter": consul_env_name,
                    }
                ),
                "owner": "consul:consul",
            },
            # There should be something that triggers this only if framework = docker
            {
                "path": "/etc/docker/compose/.env_caddy",
                "content": textwrap.dedent(
                    f"""\
                    EDXAPP_LMS_URL={edxapp_domains["lms"]}
                    EDXAPP_LMS_PREVIEW_URL={edxapp_domains["preview"]}
                    EDXAPP_CMS_URL={edxapp_domains["studio"]}
                    """
                ),
                "owner": "root:root",
                "permissions": "0664",
            },
            {
                "path": "/etc/default/vector",
                "content": textwrap.dedent(
                    f"""\
                    ENVIRONMENT={consul_env_name}
                    APPLICATION=edxapp
                    SERVICE=openedx
                    VECTOR_CONFIG_DIR=/etc/vector/
                    VECTOR_STRICT_ENV_VARS=false
                    GRAFANA_CLOUD_API_KEY={grafana_credentials["api_key"]}
                    GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials["prometheus_user_id"]}
                    GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials["loki_user_id"]}
                    """
                ),
                "owner": "root:root",
            },
            {
                "path": "/etc/default/consul-template",
                "content": f"ENVIRONMENT={consul_env_name}",
            },
            {
                "path": "/etc/default/environment-tier",
                "content": f"{environment_tier}",
            },
        ]
    }
    return base64.b64encode(
        "#cloud-config\n{}".format(
            yaml.dump(
                cloud_config_content,
                sort_keys=True,
            )
        ).encode("utf8")
    ).decode("utf8")


##############
# S3 Buckets #
##############

edxapp_mfe_bucket_name = f"{env_name}-edxapp-mfe"
edxapp_mfe_bucket = s3.Bucket(
    "edxapp-mfe-s3-bucket",
    bucket=edxapp_mfe_bucket_name,
    tags=aws_config.tags,
)
edxapp_mfe_bucket_ownership_controls = s3.BucketOwnershipControls(
    "edxapp-mfe-ownership-controls",
    bucket=edxapp_mfe_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)
s3.BucketVersioning(
    "edxapp-mfe-bucket-versioning",
    bucket=edxapp_mfe_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
        status="Suspended"
    ),
)
edxapp_mfe_bucket_public_access = s3.BucketPublicAccessBlock(
    "edxapp-mfe-bucket-public-access-controls",
    bucket=edxapp_mfe_bucket.id,
    block_public_policy=False,
)
s3.BucketPolicy(
    "edxapp-mfe-bucket-policy",
    bucket=edxapp_mfe_bucket.id,
    policy=lint_iam_policy(
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
    opts=ResourceOptions(
        depends_on=[
            edxapp_mfe_bucket_public_access,
            edxapp_mfe_bucket_ownership_controls,
        ]
    ),
)
s3.BucketCorsConfiguration(
    "edxapp-mfe-bucket-cors-rules",
    bucket=edxapp_mfe_bucket.id,
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
)


storage_bucket_name = f"{env_name}-edxapp-storage"
edxapp_storage_bucket = s3.Bucket(
    "edxapp-storage-s3-bucket",
    bucket=storage_bucket_name,
    tags=aws_config.tags,
)
edxapp_storage_bucket_ownership_controls = s3.BucketOwnershipControls(
    "edxapp-storage-ownership-controls",
    bucket=edxapp_storage_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)
s3.BucketVersioning(
    "edxapp-storage-bucket-versioning",
    bucket=edxapp_storage_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
        status="Enabled"
    ),
)
edxapp_storage_bucket_public_access = s3.BucketPublicAccessBlock(
    "edxapp-storage-bucket-public-access-controls",
    bucket=edxapp_storage_bucket.id,
    block_public_policy=False,
)
s3.BucketPolicy(
    "edxapp-storage-bucket-policy",
    bucket=edxapp_storage_bucket.id,
    policy=lint_iam_policy(
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
    opts=ResourceOptions(
        depends_on=[
            edxapp_storage_bucket_public_access,
            edxapp_storage_bucket_ownership_controls,
        ]
    ),
)
s3.BucketCorsConfiguration(
    "edxapp-storage-bucket-cors-rules",
    bucket=edxapp_storage_bucket.id,
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_headers=["*"],
            allowed_methods=["GET", "PUT", "POST", "HEAD"],
            allowed_origins=[f"https://{domain}" for domain in edxapp_domains.values()],
            expose_headers=["ETag"],
            max_age_seconds=3000,
        )
    ],
)

course_bucket_name = f"{env_name}-edxapp-courses"
edxapp_course_bucket = s3.Bucket(
    "edxapp-courses-s3-bucket",
    bucket=course_bucket_name,
    tags=aws_config.tags,
)
s3.BucketVersioning(
    "edxapp-course-bucket-versioning",
    bucket=edxapp_course_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
        status="Suspended"
    ),
)

grades_bucket_name = f"{env_name}-edxapp-grades"
edxapp_grades_bucket = s3.Bucket(
    "edxapp-grades-s3-bucket",
    bucket=grades_bucket_name,
    tags=aws_config.tags,
)
s3.BucketVersioning(
    "edxapp-grades-bucket-versioning",
    bucket=edxapp_grades_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
        status="Enabled"
    ),
)


tracking_bucket_name = f"{env_name}-edxapp-tracking"
edxapp_tracking_bucket = s3.Bucket(
    "edxapp-tracking-logs-s3-bucket",
    bucket=tracking_bucket_name,
    tags=aws_config.tags,
)
edxapp_mfe_bucket_ownership_controls = s3.BucketOwnershipControls(
    "edxapp-tracking-logs-bucket-ownership-controls",
    bucket=edxapp_tracking_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)
edxapp_tracking_bucket_public_access = s3.BucketPublicAccessBlock(
    "edxapp-tracking-logs-bucket-public-access-controls",
    bucket=edxapp_tracking_bucket.id,
    block_public_policy=True,
)
edxapp_tracking_bucket_encryption = s3.BucketServerSideEncryptionConfiguration(
    "edxapp-tracking-logs-s3-bucket-encryption",
    bucket=edxapp_tracking_bucket.id,
    rules=[
        s3.BucketServerSideEncryptionConfigurationRuleArgs(
            apply_server_side_encryption_by_default=s3.BucketServerSideEncryptionConfigurationRuleApplyServerSideEncryptionByDefaultArgs(
                sse_algorithm="aws:kms",
                kms_master_key_id=kms_s3_key["id"],
            ),
            bucket_key_enabled=True,
        ),
    ],
)
s3.BucketVersioning(
    "edxapp-tracking-logs-bucket-versioning",
    bucket=edxapp_tracking_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
        status="Enabled"
    ),
)
s3.BucketPublicAccessBlock(
    "edxapp-tracking-bucket-prevent-public-access",
    bucket=edxapp_tracking_bucket.bucket,
    block_public_acls=True,
    block_public_policy=True,
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

edxapp_iam_role = iam.Role(
    "edxapp-instance-role",
    assume_role_policy=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {"Service": "ec2.amazonaws.com"},
            },
        }
    ),
    name_prefix=f"edxapp-role-{env_name}-"[:32],
    path=f"/ol-applications/edxapp/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    tags=aws_config.merged_tags({"Name": f"{env_name}-edxapp-role"}),
)
iam.RolePolicyAttachment(
    "edxapp-describe-instances-permission",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=edxapp_iam_role.name,
)
iam.RolePolicyAttachment(
    "edxapp-role-policy",
    policy_arn=edxapp_policy.arn,
    role=edxapp_iam_role.name,
)
edxapp_instance_profile = iam.InstanceProfile(
    f"edxapp-instance-profile-{stack_info.env_suffix}",
    name_prefix=f"{stack_info.env_prefix}-edxapp-role-{stack_info.env_suffix}-",
    role=edxapp_iam_role.name,
    path=f"/ol-applications/edxapp/{stack_info.env_prefix}/",
)
##################################
#     Network Access Control     #
##################################
group_name = f"edxapp-{env_name}"
edxapp_security_group = ec2.SecurityGroup(
    "edxapp-security-group",
    name_prefix=f"{group_name}-",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            from_port=18040,
            to_port=18040,
            cidr_blocks=[
                edxapp_vpc["cidr"],
            ],
            protocol="tcp",
            description=(
                "Allow traffic to the Xqueue process running on the edxapp instances"
            ),
        ),
    ],
    egress=default_egress_args,
    tags=aws_config.merged_tags({"Name": group_name}),
    vpc_id=edxapp_vpc_id,
)

# Create security group for edxapp MariaDB database
edxapp_db_security_group = ec2.SecurityGroup(
    f"edxapp-db-access-{stack_info.env_suffix}",
    name_prefix=f"edxapp-db-access-{env_name}-",
    description="Access from Edxapp instances to the associated MariaDB database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                edxapp_security_group.id,
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
    vpc_id=edxapp_vpc_id,
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
# Register edX Platform AMI for Vault AWS auth
aws_vault_backend = f"aws-{stack_info.env_prefix}"
edxapp_web_vault_auth_role = vault.aws.AuthBackendRole(
    "edxapp-web-ami-ec2-vault-auth",
    backend=aws_vault_backend,
    auth_type="iam",
    role="edxapp-web",
    inferred_entity_type="ec2_instance",
    inferred_aws_region="us-east-1",
    bound_iam_instance_profile_arns=[edxapp_instance_profile.arn],
    bound_ami_ids=[edxapp_web_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[edxapp_vpc_id],
    token_policies=[edxapp_vault_policy.name],
)

edxapp_worker_vault_auth_role = vault.aws.AuthBackendRole(
    "edxapp-worker-ami-ec2-vault-auth",
    backend=aws_vault_backend,
    auth_type="iam",
    role="edxapp-worker",
    inferred_entity_type="ec2_instance",
    inferred_aws_region="us-east-1",
    bound_iam_instance_profile_arns=[edxapp_instance_profile.arn],
    bound_ami_ids=[edxapp_worker_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[edxapp_vpc_id],
    token_policies=[edxapp_vault_policy.name],
)

edx_notes_vault_policy = vault.Policy(
    "edx-notes-api-vault-policy",
    name=f"edx-notes-api-{stack_info.env_suffix}",
    policy=Path(__file__).parent.joinpath("edx_notes_api_policy.hcl").read_text(),
)
edxapp_notes_iam_role = iam.Role(
    "edx-notes-api-iam-role",
    assume_role_policy=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
            },
        }
    ),
    name_prefix=f"edx-notes-role-{env_name}-"[:IAM_ROLE_NAME_PREFIX_MAX_LENGTH],
    path=f"/ol-applications/edx-notes-api/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    tags=aws_config.merged_tags({"Name": f"{env_name}-edx-notes-api-role"}),
)
edxapp_notes_vault_auth_role = vault.aws.AuthBackendRole(
    "edx-notes-iam-vault-auth",
    backend=aws_vault_backend,
    auth_type="iam",
    role="edx-notes-api",
    resolve_aws_unique_ids=True,
    token_policies=[edx_notes_vault_policy.name],
    bound_iam_principal_arns=[edxapp_notes_iam_role.arn],
)

##########################
#     Database Setup     #
##########################
if (
    edxapp_config.get_bool("k8s_deployment")
    and edxapp_config.get_bool("k8s_cutover")
    and edxapp_config.get_bool("disable_ec2_deployment")
):
    rds_subnet = k8s_vpc["rds_subnet"]
else:
    rds_subnet = edxapp_vpc["rds_subnet"]

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

edxapp_db_consul_node = Node(
    "edxapp-instance-db-node",
    name="edxapp-mysql",
    address=edxapp_db.db_instance.address,
    opts=consul_provider,
)

edxapp_db_consul_service = Service(
    "edxapp-instance-db-service",
    node=edxapp_db_consul_node.name,
    name="edxapp-db",
    port=edxapp_db_config.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="edxapp-db",
            interval="10s",
            name="edxapp-db",
            timeout="1m0s",
            status="passing",
            tcp=Output.all(
                address=edxapp_db.db_instance.address, port=edxapp_db_config.port
            ).apply(lambda db: "{address}:{port}".format(**db)),
        )
    ],
    opts=consul_provider,
)
if edxapp_db_config.read_replica:
    edxapp_db_replica_consul_service = Service(
        "edxapp-instance-db-replica-service",
        node=edxapp_db_consul_node.name,
        name="edxapp-db-replica",
        port=edxapp_db_config.port,
        meta={
            "external-node": True,
            "external-probe": True,
        },
        checks=[
            ServiceCheckArgs(
                check_id="edxapp-db",
                interval="10s",
                name="edxapp-db",
                timeout="1m0s",
                status="passing",
                tcp=Output.all(
                    address=edxapp_db.db_replica.address, port=edxapp_db_config.port
                ).apply(lambda db: "{address}:{port}".format(**db)),
            )
        ],
        opts=consul_provider,
    )

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
            security_groups=[
                edxapp_security_group.id,
            ],
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
edxapp_redis_consul_node = Node(
    "edxapp-redis-cache-node",
    name="edxapp-redis",
    address=edxapp_redis_cache.address,
    opts=consul_provider,
)

edxapp_redis_consul_service = Service(
    "edxapp-redis-consul-service",
    node=edxapp_redis_consul_node.name,
    name="edxapp-redis",
    port=redis_cache_config.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="edxapp-redis",
            interval="10s",
            name="edxapp-redis",
            timeout="1m0s",
            status="passing",
            tcp=Output.all(
                address=edxapp_redis_cache.address,
                port=edxapp_redis_cache.cache_cluster.port,
            ).apply(lambda cluster: "{address}:{port}".format(**cluster)),
        )
    ],
    opts=consul_provider,
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
            default_value="default",
            dimension_name=f"edxapp-{env_name}",
            value_source="emailHeader",
        )
    ],
)

######################
# Build waffle flags
######################
waffle_list = edxapp_config.get_object("waffle_flags", default=[])
waffle_flags_yaml_content = yaml.safe_dump({"waffles": waffle_list})

######################
# Manage Consul Data #
######################
consul_kv_data = {
    "enable_notes": (
        "true" if edxapp_config.get_bool("enable_notes") else "false"
    ),  # intended quoted boolean
    # TMM 2023-12-06 - This is deprecated and can be removed once Quince makes it to
    # xPro production
    "default-module-class": edxapp_config.get("default_module_class")
    or "xmodule.hidden_block.HiddenBlock",
    "google-analytics-id": edxapp_config.require("google_analytics_id"),
    "github-auto-export-org-api-url": edxapp_config.get("github_org_api_url") or "",
    "lms-domain": edxapp_domains["lms"],
    "marketing-domain": edxapp_config.get("marketing_domain") or "",
    "mitxonline-domain": edxapp_config.get("mitxonline_domain") or "",
    "mit-learn-domain": edxapp_config.get("mit_learn_domain") or "",
    "preview-domain": edxapp_domains["preview"],
    "rds-host": edxapp_db.db_instance.address,
    "proctortrack-base-url": edxapp_config.get("proctortrack_url") or "",
    "s3-course-bucket": course_bucket_name,
    "s3-grades-bucket": grades_bucket_name,
    "s3-storage-bucket": storage_bucket_name,
    "sender-email-address": edxapp_config.require("sender_email_address"),
    "email-use-course-id-from-for-bulk": (
        "true"
        if edxapp_config.get_bool("email_use_course_id_from_for_bulk")
        else "false"
    ),
    "bulk-email-default-from-email": (
        edxapp_config.get("bulk_email_default_from_email")
        or edxapp_config.require("sender_email_address")
    ),
    "ses-configuration-set": f"edxapp-{env_name}",
    "ses-mail-domain": edxapp_mail_domain,
    "session-cookie-domain": ".{}".format(edxapp_domains["lms"].split(".", 1)[-1]),
    "studio-domain": edxapp_domains["studio"],
    "waffle_flags.yaml": waffle_flags_yaml_content,
}
consul.Keys(
    "edxapp-consul-template-data",
    keys=[
        consul.KeysKeyArgs(path=f"edxapp/{key}", value=config_value)
        for key, config_value in consul_kv_data.items()
    ],
    opts=consul_provider,
)

##########################
#     EC2 Deployment     #
##########################

if not edxapp_config.get_bool("disable_ec2_deployment"):
    # Create load balancer for Edxapp web nodes
    edxapp_web_tag = f"edxapp-web-{env_name}"
    edxapp_worker_tag = f"edxapp-worker-{env_name}"
    web_lb = lb.LoadBalancer(
        "edxapp-web-load-balancer",
        name=edxapp_web_tag[:AWS_LOAD_BALANCER_NAME_MAX_LENGTH],
        ip_address_type="dualstack",
        load_balancer_type="application",
        enable_http2=True,
        subnets=edxapp_vpc["subnet_ids"],
        security_groups=[
            edxapp_vpc["security_groups"]["web"],
        ],
        tags=aws_config.merged_tags({"Name": edxapp_web_tag}),
    )

    TARGET_GROUP_NAME_MAX_LENGTH = 32
    lms_web_lb_target_group = lb.TargetGroup(
        "edxapp-web-lms-alb-target-group",
        vpc_id=edxapp_vpc_id,
        target_type="instance",
        port=DEFAULT_HTTPS_PORT,
        protocol="HTTPS",
        health_check=lb.TargetGroupHealthCheckArgs(
            healthy_threshold=3,
            timeout=10,
            interval=edxapp_config.get_int("elb_healthcheck_interval") or 30,
            path="/heartbeat",
            port=str(DEFAULT_HTTPS_PORT),
            protocol="HTTPS",
        ),
        name_prefix=f"lms-{stack_info.env_suffix}-"[:6],
        tags=aws_config.tags,
        preserve_client_ip=True,
        load_balancing_algorithm_type="least_outstanding_requests",
    )
    # Studio has some workflows that are stateful, such as importing and exporting courses
    # which requires files to be written and read from the same EC2 instance. This adds
    # separate target groups and ALB listener rules to route requests for studio to a target
    # group with session stickiness enabled so that these stateful workflows don't fail.
    # TMM 2021-07-20
    studio_web_lb_target_group = lb.TargetGroup(
        "edxapp-web-studio-alb-target-group",
        vpc_id=edxapp_vpc_id,
        target_type="instance",
        port=DEFAULT_HTTPS_PORT,
        protocol="HTTPS",
        health_check=lb.TargetGroupHealthCheckArgs(
            healthy_threshold=3,
            timeout=10,
            interval=30,
            path="/heartbeat",
            port=str(DEFAULT_HTTPS_PORT),
            protocol="HTTPS",
        ),
        stickiness=lb.TargetGroupStickinessArgs(
            type="lb_cookie",
            enabled=True,
        ),
        name_prefix=f"studio-{stack_info.env_suffix}-"[:6],
        tags=aws_config.tags,
    )
    edxapp_web_acm_cert = acm.Certificate(
        "edxapp-load-balancer-acm-certificate",
        domain_name=edxapp_domains["lms"],
        subject_alternative_names=[
            domain for key, domain in edxapp_domains.items() if key != "lms"
        ],
        validation_method="DNS",
        tags=aws_config.tags,
    )

    edxapp_acm_cert_validation_records = (
        edxapp_web_acm_cert.domain_validation_options.apply(
            partial(
                acm_certificate_validation_records,
                cert_name="edx-platform",
                zone_id=edxapp_zone_id,
                stack_info=stack_info,
            )
        )
    )

    edxapp_web_acm_validated_cert = acm.CertificateValidation(
        "wait-for-edxapp-acm-cert-validation",
        certificate_arn=edxapp_web_acm_cert.arn,
        validation_record_fqdns=edxapp_acm_cert_validation_records.apply(
            lambda validation_records: [
                validation_record.fqdn for validation_record in validation_records
            ]
        ),
    )
    edxapp_web_alb_http_listener = lb.Listener(
        "edxapp-web--alb-listener-http",
        load_balancer_arn=web_lb.arn,
        port=DEFAULT_HTTP_PORT,
        protocol="HTTP",
        default_actions=[
            lb.ListenerDefaultActionArgs(
                type="forward",
                target_group_arn=lms_web_lb_target_group.arn,
            )
        ],
        opts=ResourceOptions(delete_before_replace=True),
    )
    edxapp_web_alb_listener = lb.Listener(
        "edxapp-web-alb-listener",
        certificate_arn=edxapp_web_acm_validated_cert.certificate_arn,
        load_balancer_arn=web_lb.arn,
        port=DEFAULT_HTTPS_PORT,
        protocol="HTTPS",
        default_actions=[
            lb.ListenerDefaultActionArgs(
                type="forward",
                target_group_arn=lms_web_lb_target_group.arn,
            )
        ],
        opts=ResourceOptions(delete_before_replace=True),
    )
    edxapp_studio_web_alb_listener_rule = lb.ListenerRule(
        "edxapp-web-studio-alb-listener-routing",
        listener_arn=edxapp_web_alb_listener.arn,
        actions=[
            lb.ListenerRuleActionArgs(
                type="forward",
                target_group_arn=studio_web_lb_target_group.arn,
            )
        ],
        conditions=[
            lb.ListenerRuleConditionArgs(
                host_header=lb.ListenerRuleConditionHostHeaderArgs(
                    values=[edxapp_domains["studio"]]
                )
            )
        ],
        priority=1,
        tags=aws_config.tags,
    )
    edxapp_lms_web_alb_listener_rule = lb.ListenerRule(
        "edxapp-web-lms-alb-listener-routing",
        listener_arn=edxapp_web_alb_listener.arn,
        actions=[
            lb.ListenerRuleActionArgs(
                type="forward",
                target_group_arn=lms_web_lb_target_group.arn,
            )
        ],
        conditions=[
            lb.ListenerRuleConditionArgs(
                host_header=lb.ListenerRuleConditionHostHeaderArgs(
                    values=[edxapp_domains["lms"]]
                )
            )
        ],
        priority=2,
        tags=aws_config.tags,
    )

    grafana_config = Config("grafana")
    cloud_init_user_data = Output.secret(
        consul_stack.require_output("datacenter").apply(cloud_init_user_data_func)
    )

    web_instance_type = (
        edxapp_config.get("web_instance_type") or InstanceTypes.high_mem_regular.name
    )
    web_launch_config = ec2.LaunchTemplate(
        "edxapp-web-launch-template",
        name_prefix=f"edxapp-web-{env_name}-",
        description=f"Launch template for deploying Edxapp web nodes in {env_name}",
        iam_instance_profile=ec2.LaunchTemplateIamInstanceProfileArgs(
            arn=edxapp_instance_profile.arn,
        ),
        image_id=edxapp_web_ami.id,
        vpc_security_group_ids=[
            edxapp_security_group.id,
            edxapp_vpc["security_groups"]["web"],
            consul_security_groups["consul_agent"],
        ],
        block_device_mappings=[
            ec2.LaunchTemplateBlockDeviceMappingArgs(
                device_name=edxapp_web_ami.root_device_name,
                ebs=ec2.LaunchTemplateBlockDeviceMappingEbsArgs(
                    volume_size=25,
                    volume_type=DiskTypes.ssd,
                    delete_on_termination=True,
                    encrypted=True,
                    kms_key_id=kms_ebs["arn"],
                ),
            ),
        ],
        instance_type=InstanceTypes.dereference(web_instance_type),
        key_name=SSH_ACCESS_KEY_NAME,
        tag_specifications=[
            ec2.LaunchTemplateTagSpecificationArgs(
                resource_type="instance",
                tags=aws_config.merged_tags({"Name": edxapp_web_tag}),
            ),
            ec2.LaunchTemplateTagSpecificationArgs(
                resource_type="volume",
                tags=aws_config.merged_tags({"Name": edxapp_web_tag}),
            ),
        ],
        tags=aws_config.tags,
        user_data=cloud_init_user_data,
    )
    web_asg = autoscaling.Group(
        "edxapp-web-autoscaling-group",
        desired_capacity=edxapp_config.get_int("web_node_capacity")
        or MIN_WEB_NODES_DEFAULT,
        min_size=edxapp_config.get_int("min_web_nodes") or MIN_WEB_NODES_DEFAULT,
        max_size=edxapp_config.get_int("max_web_nodes") or MAX_WEB_NODES_DEFAULT,
        health_check_type="ELB",
        vpc_zone_identifiers=edxapp_vpc["subnet_ids"],
        launch_template=autoscaling.GroupLaunchTemplateArgs(
            id=web_launch_config.id, version="$Latest"
        ),
        instance_refresh=autoscaling.GroupInstanceRefreshArgs(
            strategy="Rolling",
            preferences=autoscaling.GroupInstanceRefreshPreferencesArgs(
                min_healthy_percentage=50
            ),
            triggers=["tag"],
        ),
        target_group_arns=[lms_web_lb_target_group.arn, studio_web_lb_target_group.arn],
        tags=[
            autoscaling.GroupTagArgs(
                key=key_name,
                value=key_value,
                propagate_at_launch=True,
            )
            for key_name, key_value in aws_config.merged_tags(
                {"ami_id": edxapp_web_ami.id, "edxapp_node_type": "web"},
            ).items()
        ],
    )

    web_asg_scale_up_policy = autoscaling.Policy(
        "edxapp-web-scale-up-policy",
        adjustment_type="PercentChangeInCapacity",
        estimated_instance_warmup=300,
        policy_type="StepScaling",
        step_adjustments=[
            autoscaling.PolicyStepAdjustmentArgs(
                scaling_adjustment=0,
                metric_interval_lower_bound=0,
                metric_interval_upper_bound=10,
            ),
            autoscaling.PolicyStepAdjustmentArgs(
                scaling_adjustment=10,
                metric_interval_lower_bound=10,
                metric_interval_upper_bound=20,
            ),
            autoscaling.PolicyStepAdjustmentArgs(
                scaling_adjustment=30,
                metric_interval_lower_bound=20,
            ),
        ],
        autoscaling_group_name=web_asg.name,
    )

    web_asg_scale_down_policy = autoscaling.Policy(
        "edxapp-web-scale-down-policy",
        adjustment_type="PercentChangeInCapacity",
        estimated_instance_warmup=300,
        policy_type="StepScaling",
        step_adjustments=[
            autoscaling.PolicyStepAdjustmentArgs(
                scaling_adjustment=0,
                metric_interval_lower_bound=-10,
                metric_interval_upper_bound=0,
            ),
            autoscaling.PolicyStepAdjustmentArgs(
                scaling_adjustment=-10,
                metric_interval_lower_bound=-20,
                metric_interval_upper_bound=-10,
            ),
            autoscaling.PolicyStepAdjustmentArgs(
                scaling_adjustment=-30,
                metric_interval_upper_bound=-20,
            ),
        ],
        autoscaling_group_name=web_asg.name,
    )

    web_alb_metric_alarm = cloudwatch.MetricAlarm(
        "edxapp-web-alb-metric-alarm",
        comparison_operator="GreaterThanOrEqualToThreshold",
        evaluation_periods=5,
        metric_name="TargetResponseTime",
        namespace="AWS/ApplicationELB",
        period=120,
        statistic="Average",
        threshold=1,
        dimensions={
            "LoadBalancer": Output.all(lb_arn=web_lb.arn_suffix).apply(
                lambda lb_attrs: f"{lb_attrs['lb_arn']}"
            ),
        },
        datapoints_to_alarm=5,
        alarm_description=(
            "Time elapsed after the request leaves the load balancer until a response from"
            " the target is received"
        ),
        alarm_actions=[web_asg_scale_up_policy.arn],
        ok_actions=[web_asg_scale_down_policy.arn],
        tags=aws_config.tags,
    )

    worker_instance_type = (
        edxapp_config.get("worker_instance_type") or InstanceTypes.burstable_medium.name
    )
    worker_launch_config = ec2.LaunchTemplate(
        "edxapp-worker-launch-template",
        name_prefix=f"{edxapp_worker_tag}-",
        description="Launch template for deploying Edxapp worker nodes",
        iam_instance_profile=ec2.LaunchTemplateIamInstanceProfileArgs(
            arn=edxapp_instance_profile.arn,
        ),
        image_id=edxapp_worker_ami.id,
        block_device_mappings=[
            ec2.LaunchTemplateBlockDeviceMappingArgs(
                device_name=edxapp_worker_ami.root_device_name,
                ebs=ec2.LaunchTemplateBlockDeviceMappingEbsArgs(
                    volume_size=edxapp_config.get_int("worker_disk_size") or 50,
                    volume_type=DiskTypes.ssd,
                    delete_on_termination=True,
                    encrypted=True,
                    kms_key_id=kms_ebs["arn"],
                ),
            )
        ],
        vpc_security_group_ids=[
            edxapp_security_group.id,
            consul_security_groups["consul_agent"],
        ],
        instance_type=InstanceTypes.dereference(worker_instance_type),
        key_name=SSH_ACCESS_KEY_NAME,
        tag_specifications=[
            ec2.LaunchTemplateTagSpecificationArgs(
                resource_type="instance",
                tags=aws_config.merged_tags({"Name": edxapp_worker_tag}),
            ),
            ec2.LaunchTemplateTagSpecificationArgs(
                resource_type="volume",
                tags=aws_config.merged_tags({"Name": edxapp_worker_tag}),
            ),
        ],
        tags=aws_config.tags,
        user_data=cloud_init_user_data,
    )
    worker_asg = autoscaling.Group(
        "edxapp-worker-autoscaling-group",
        desired_capacity=edxapp_config.get_int("worker_node_capacity") or 1,
        min_size=1,
        max_size=50,
        health_check_type="EC2",
        vpc_zone_identifiers=edxapp_vpc["subnet_ids"],
        launch_template=autoscaling.GroupLaunchTemplateArgs(
            id=worker_launch_config.id, version="$Latest"
        ),
        instance_refresh=autoscaling.GroupInstanceRefreshArgs(
            strategy="Rolling",
            preferences=autoscaling.GroupInstanceRefreshPreferencesArgs(
                min_healthy_percentage=50
            ),
            triggers=["tag"],
        ),
        tags=[
            autoscaling.GroupTagArgs(
                key=key_name,
                value=key_value,
                propagate_at_launch=True,
            )
            for key_name, key_value in aws_config.merged_tags(
                {"ami_id": edxapp_worker_ami.id, "edxapp_node_type": "worker"},
            ).items()
        ],
    )

########################
# Fastly CDN Managment #
########################

if edxapp_config.get_bool("k8s_cutover"):
    lms_backend_address = edxapp_config.require("backend_lms_domain")
    lms_backend_ssl_hostname = edxapp_config.require("backend_lms_domain")
    cms_backend_address = edxapp_config.require("backend_studio_domain")
    cms_backend_ssl_hostname = edxapp_config.require("backend_studio_domain")
    preview_backend_address = edxapp_config.require("backend_preview_domain")
    preview_backend_ssl_hostname = edxapp_config.require("backend_preview_domain")
else:
    lms_backend_address = web_lb.dns_name
    lms_backend_ssl_hostname = edxapp_domains["lms"]
    cms_backend_address = web_lb.dns_name
    cms_backend_ssl_hostname = edxapp_domains["studio"]
    preview_backend_address = web_lb.dns_name
    preview_backend_ssl_hostname = edxapp_domains["preview"]


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
            address=edxapp_mfe_bucket.bucket_domain_name,
            name="MFE S3 Bucket",
            override_host=edxapp_mfe_bucket.bucket_domain_name,
            port=DEFAULT_HTTPS_PORT,
            request_condition="MFE Path",
            ssl_cert_hostname=edxapp_mfe_bucket.bucket_domain_name,
            ssl_sni_hostname=edxapp_mfe_bucket.bucket_domain_name,
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
            first_byte_timeout=300000,
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
            format=consul_stack.require_output("datacenter").apply(
                lambda dc: build_fastly_log_format_string(
                    additional_static_fields={
                        "application": "edxapp",
                        "environment": dc,
                    }
                )
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
if edxapp_config.get_bool("k8s_deployment"):
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
    "edx_notes_iam_role": edxapp_notes_iam_role.arn,
    "deployment": stack_info.env_prefix,
}
if not edxapp_config.get_bool("disable_ec2_deployment"):
    export_dict["load_balancer"] = {"dns_name": web_lb.dns_name, "arn": web_lb.arn}

export("edxapp", export_dict)

export("edxapp_security_group", edxapp_security_group.id)

if edxapp_db_config.read_replica:
    export("edxapp_read_replica", edxapp_db.db_replica.address)
