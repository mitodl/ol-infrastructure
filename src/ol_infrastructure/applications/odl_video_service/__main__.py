"""The complete state needed to provision OVS running on Docker."""

import base64
import json
import os
import textwrap
from itertools import chain
from pathlib import Path

import pulumi_consul as consul
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
import yaml
from pulumi import (
    ROOT_STACK_RESOURCE,
    Alias,
    Config,
    Output,
    ResourceOptions,
    StackReference,
    export,
)
from pulumi_aws import ec2, get_caller_identity, iam, route53
from pulumi_consul import Node, Service, ServiceCheckArgs

from bridge.lib.magic_numbers import (
    AWS_RDS_DEFAULT_DATABASE_CAPACITY,
    DEFAULT_HTTP_PORT,
    DEFAULT_HTTPS_PORT,
    DEFAULT_POSTGRES_PORT,
    DEFAULT_REDIS_PORT,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.aws.auto_scale_group import (
    BlockDeviceMapping,
    OLAutoScaleGroupConfig,
    OLAutoScaling,
    OLLaunchTemplateConfig,
    OLLoadBalancerConfig,
    OLTargetGroupConfig,
    TagSpecification,
)
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.mediaconvert import (
    MediaConvertConfig,
    OLMediaConvert,
)
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.services.k8s import (
    OLApplicationK8s,
    OLApplicationK8sCeleryWorkerConfig,
    OLApplicationK8sConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import InstanceTypes, default_egress_args
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.consul import consul_key_helper, get_consul_provider
from ol_infrastructure.lib.ol_types import (
    Application,
    AWSBase,
    BusinessUnit,
    K8sAppLabels,
    Product,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

# Docker image tag from environment variable (required for K8s deployments)
if "ODL_VIDEO_DOCKER_TAG" not in os.environ:
    msg = "ODL_VIDEO_DOCKER_TAG environment variable must be set"
    raise ValueError(msg)
ODL_VIDEO_DOCKER_TAG = os.environ["ODL_VIDEO_DOCKER_TAG"]

# Configuration items and initialziations
if Config("vault_server").get("env_namespace"):
    setup_vault_provider()
ovs_config = Config("ovs")
vault_config = Config("vault")
redis_config = Config("redis")
stack_info = parse_stack()

aws_account = get_caller_identity()

network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(f"infrastructure.consul.apps.{stack_info.name}")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")

target_vpc_name = ovs_config.get("target_vpc") or f"{stack_info.env_prefix}_vpc"
target_vpc = network_stack.require_output(target_vpc_name)
target_vpc_id = target_vpc["id"]
data_vpc = network_stack.require_output("data_vpc")

mitodl_zone_id = dns_stack.require_output("odl_zone_id")

# We will take the entire secret structure and load it into Vault as is under
# the root mount further down in the file.
secrets = read_yaml_secrets(
    Path(f"odl_video_service/data.{stack_info.env_suffix}.yaml")
)

aws_config = AWSBase(
    tags={
        "OU": "odl-video",
        "Environment": f"applications_{stack_info.env_suffix}",
    }
)
consul_provider = get_consul_provider(stack_info)

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

# S3 Buckets for ODL Video Service
# Get bucket names from configuration
s3_bucket_name = ovs_config.require("s3_bucket_name")
s3_subtitle_bucket_name = ovs_config.require("s3_subtitle_bucket_name")
s3_thumbnail_bucket_name = ovs_config.require("s3_thumbnail_bucket_name")
s3_transcode_bucket_name = ovs_config.require("s3_transcode_bucket_name")
s3_watch_bucket_name = ovs_config.require("s3_watch_bucket_name")

# Main S3 bucket (video files)
ovs_main_bucket_config = S3BucketConfig(
    bucket_name=s3_bucket_name,
    versioning_enabled=False,
    server_side_encryption_enabled=True,
    intelligent_tiering_enabled=True,
    intelligent_tiering_days=90,
    tags=aws_config.merged_tags(
        {"Name": s3_bucket_name, "Application": "odl-video-service"}
    ),
)
ovs_main_bucket = OLBucket(
    "ovs-main-bucket",
    ovs_main_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(
                name="ovs-main-bucket-bucket",
                parent=ROOT_STACK_RESOURCE,
            ),
        ]
    ),
)

# Subtitles bucket
ovs_subtitles_bucket_config = S3BucketConfig(
    bucket_name=s3_subtitle_bucket_name,
    versioning_enabled=False,
    server_side_encryption_enabled=True,
    sse_algorithm="AES256",  # Use SSE-S3 for CloudFront compatibility
    intelligent_tiering_enabled=True,
    intelligent_tiering_days=90,
    tags=aws_config.merged_tags(
        {"Name": s3_subtitle_bucket_name, "Application": "odl-video-service"}
    ),
)
ovs_subtitles_bucket = OLBucket(
    "ovs-subtitles-bucket",
    ovs_subtitles_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(
                name="ovs-subtitles-bucket-bucket",
                parent=ROOT_STACK_RESOURCE,
            ),
        ]
    ),
)

# Thumbnails bucket
ovs_thumbnails_bucket_config = S3BucketConfig(
    bucket_name=s3_thumbnail_bucket_name,
    versioning_enabled=False,
    server_side_encryption_enabled=True,
    sse_algorithm="AES256",  # Use SSE-S3 for CloudFront compatibility
    intelligent_tiering_enabled=True,
    intelligent_tiering_days=90,
    tags=aws_config.merged_tags(
        {"Name": s3_thumbnail_bucket_name, "Application": "odl-video-service"}
    ),
)
ovs_thumbnails_bucket = OLBucket(
    "ovs-thumbnails-bucket",
    ovs_thumbnails_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(
                name="ovs-thumbnails-bucket-bucket",
                parent=ROOT_STACK_RESOURCE,
            ),
        ]
    ),
)

# Transcoded bucket (allows public access for CloudFront)
# Uses SSE-S3 (AES256) instead of KMS because CloudFront cannot access KMS-encrypted
# objects without explicit KMS key policy permissions
ovs_transcoded_bucket_config = S3BucketConfig(
    bucket_name=s3_transcode_bucket_name,
    versioning_enabled=False,
    server_side_encryption_enabled=True,
    sse_algorithm="AES256",  # Use SSE-S3 for CloudFront compatibility
    intelligent_tiering_enabled=True,
    intelligent_tiering_days=90,
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
    restrict_public_buckets=False,
    tags=aws_config.merged_tags(
        {"Name": s3_transcode_bucket_name, "Application": "odl-video-service"}
    ),
)
ovs_transcoded_bucket = OLBucket(
    "ovs-transcoded-bucket",
    ovs_transcoded_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(
                name="ovs-transcoded-bucket-bucket",
                parent=ROOT_STACK_RESOURCE,
            ),
        ]
    ),
)

# Uploaded bucket (user uploads)
ovs_uploaded_bucket_config = S3BucketConfig(
    bucket_name=s3_watch_bucket_name,
    versioning_enabled=False,
    server_side_encryption_enabled=True,
    intelligent_tiering_enabled=True,
    intelligent_tiering_days=90,
    tags=aws_config.merged_tags(
        {"Name": s3_watch_bucket_name, "Application": "odl-video-service"}
    ),
)
ovs_uploaded_bucket = OLBucket(
    "ovs-uploaded-bucket",
    ovs_uploaded_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(
                name="ovs-uploaded-bucket-bucket",
                parent=ROOT_STACK_RESOURCE,
            ),
        ]
    ),
)

# IAM and instance profile

parliament_config = {
    "PERMISSIONS_MANAGEMENT_ACTIONS": {
        "ignore_locations": [{"actions": ["s3:putobjectacl"]}]
    },
    "UNKNOWN_ACTION": {"ignore_locations": []},
    "RESOURCE_EFFECTIVELY_STAR": {"ignore_locations": []},
}

# Get the standard MediaConvert policy statements
mediaconvert_policy_statements = OLMediaConvert.get_standard_policy_statements(
    stack_info.env_suffix, service_name="odl-video"
)

ovs_server_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Action": ["s3:ListBucket", "s3:HeadObject", "s3:GetObject"],
            "Effect": "Allow",
            "Resource": [
                "arn:aws:s3:::ttv_videos",
                "arn:aws:s3:::ttv_videos/*",
                "arn:aws:s3:::ttv_static",
                "arn:aws:s3:::ttv_static/*",
            ],
        },
        {
            "Action": [
                "elastictranscoder:CancelJob",
                "elastictranscoder:ReadJob",
            ],
            "Effect": "Allow",
            "Resource": [
                f"arn:aws:elastictranscoder:{aws_config.region}:{aws_account.id}:job/*"
            ],
        },
        {
            "Action": [
                "elastictranscoder:ListJobsByPipeline",
                "elastictranscoder:ReadPipeline",
            ],
            "Effect": "Allow",
            "Resource": [
                f"arn:aws:elastictranscoder:{aws_config.region}:{aws_account.id}:pipeline/{secrets['misc']['et_pipeline_id']}"
            ],
        },
        {
            "Action": [
                "elastictranscoder:CreateJob",
            ],
            "Effect": "Allow",
            "Resource": [
                f"arn:aws:elastictranscoder:{aws_config.region}:{aws_account.id}:preset/*",
                f"arn:aws:elastictranscoder:{aws_config.region}:{aws_account.id}:pipeline/{secrets['misc']['et_pipeline_id']}",
            ],
        },
        {
            "Action": [
                "elastictranscoder:ReadPreset",
            ],
            "Effect": "Allow",
            "Resource": [
                f"arn:aws:elastictranscoder:{aws_config.region}:{aws_account.id}:preset/*",
            ],
        },
        # This block against odl-video-service* buckets is REQUIRED
        # App does not work without it?????
        # TODO MAD 20221115 Why is it required?  # noqa: FIX002, TD002, TD004
        # The S3 permissions block following this SHOULD cover what this provides
        # but the app must be making some kind of call to bucket that isn't qualified
        # by the environment (CI,RC,Production)
        # There are 21 odl-video-service* buckets at the moment.
        {
            "Action": [
                "s3:HeadObject",
                "s3:GetObject",
                "s3:ListAllMyBuckets",
                "s3:ListBucket",
                "s3:ListObjects",
                "s3:PutObject",
                "s3:DeleteObject",
            ],
            "Effect": "Allow",
            "Resource": [
                "arn:aws:s3:::odl-video-service*",
                "arn:aws:s3:::odl-video-service*/*",
            ],
        },
        {
            "Action": ["sns:ListSubscriptionsByTopic", "sns:Publish"],
            "Effect": "Allow",
            "Resource": [
                f"arn:aws:sns:{aws_config.region}:{aws_account.id}:odl-video-service"
            ],
        },
        {
            "Action": [
                "s3:DeleteObject",
                "s3:DeleteObjectVersion",
                "s3:GetAccelerateConfiguration",
                "s3:GetBucketAcl",
                "s3:GetBucketCORS",
                "s3:GetBucketLocation",
                "s3:GetBucketLogging",
                "s3:GetBucketNotification",
                "s3:GetBucketPolicy",
                "s3:GetBucketTagging",
                "s3:GetBucketVersioning",
                "s3:GetBucketWebsite",
                "s3:GetLifecycleConfiguration",
                "s3:GetObject",
                "s3:GetObjectAcl",
                "s3:GetObjectTagging",
                "s3:GetObjectTorrent",
                "s3:GetObjectVersion",
                "s3:GetObjectVersionAcl",
                "s3:GetObjectVersionTagging",
                "s3:GetObjectVersionTorrent",
                "s3:GetReplicationConfiguration",
                "s3:HeadObject",
                "s3:ListAllMyBuckets",
                "s3:ListObjects",
                "s3:ListBucket",
                "s3:ListBucketMultipartUploads",
                "s3:ListBucketVersions",
                "s3:ListMultipartUploadParts",
                "s3:PutBucketWebsite",
                "s3:PutObject",
                "s3:PutObjectTagging",
                "s3:ReplicateDelete",
                "s3:ReplicateObject",
                "s3:RestoreObject",
            ],
            "Effect": "Allow",
            "Resource": [
                f"arn:aws:s3:::{ovs_config.get('s3_bucket_name')}/",
                f"arn:aws:s3:::{ovs_config.get('s3_subtitle_bucket_name')}/",
                f"arn:aws:s3:::{ovs_config.get('s3_thumbnail_bucket_name')}/",
                f"arn:aws:s3:::{ovs_config.get('s3_transcode_bucket_name')}/",
                f"arn:aws:s3:::{ovs_config.get('s3_watch_bucket_name')}/",
                f"arn:aws:s3:::{ovs_config.get('s3_bucket_name')}/*",
                f"arn:aws:s3:::{ovs_config.get('s3_subtitle_bucket_name')}/*",
                f"arn:aws:s3:::{ovs_config.get('s3_thumbnail_bucket_name')}/*",
                f"arn:aws:s3:::{ovs_config.get('s3_transcode_bucket_name')}/*",
                f"arn:aws:s3:::{ovs_config.get('s3_watch_bucket_name')}/*",
            ],
        },
        # Include standard MediaConvert policy statements
        *mediaconvert_policy_statements,
    ],
}
ovs_server_policy = iam.Policy(
    "odl-video-service-server-policy",
    name_prefix="odl-video-service-server-policy-",
    path=f"/ol-applications/odl-video-service-server/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        ovs_server_policy_document,
        stringify=True,
        parliament_config=parliament_config,
    ),
    description=(
        "AWS access permissions to allow odl-video-service to s3 buckets and transcode"
        " services."
    ),
)

ovs_server_instance_role = iam.Role(
    f"odl-video-service-server-instance-role-{stack_info.env_suffix}",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Principal": {"Service": "ec2.amazonaws.com"},
                }
            ],
        }
    ),
    path="/ol-infrastructure/odl-video-service-server/role/",
    tags=aws_config.tags,
)

iam.RolePolicyAttachment(
    f"odl-video-service-server-describe-instance-role-policy-{env_name}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=ovs_server_instance_role.name,
)

iam.RolePolicyAttachment(
    f"odl-video-service-server-s3-transcode-role-policy-{env_name}",
    policy_arn=ovs_server_policy.arn,
    role=ovs_server_instance_role.name,
)

ovs_server_instance_profile = iam.InstanceProfile(
    f"odl-video-service-server-instance-profile-{env_name}",
    role=ovs_server_instance_role.name,
    path="/ol-infrastructure/odl-video-service-server/profile/",
)

# Need to pin the same policy that the instance profile will use to the aws auth
# backend because the app still needs an AWS Key
ocw_studio_vault_backend_role = vault.aws.SecretBackendRole(
    f"ovs-server-{stack_info.env_suffix}",
    name="ovs-server",
    backend="aws-mitx",
    credential_type="iam_user",
    iam_tags={"OU": "operations", "vault_managed": "True"},
    policy_arns=[
        policy_stack.require_output("iam_policies")["describe_instances"],
        ovs_server_policy.arn,
    ],
    opts=ResourceOptions(delete_before_replace=True),
)

# Network Access Control

# Create various security groups
ovs_server_security_group = ec2.SecurityGroup(
    f"odl-video-service-server-security-group-{env_name}",
    name=f"odl-video-service-server-{target_vpc_name}-{env_name}",
    description="Access control for odl-video-service servers",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_HTTPS_PORT,
            to_port=DEFAULT_HTTPS_PORT,
            cidr_blocks=["0.0.0.0/0"],
            description=(
                "Allow traffic to the odl-video-service server on port"
                f" {DEFAULT_HTTPS_PORT}"
            ),
        ),
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_HTTP_PORT,
            to_port=DEFAULT_HTTP_PORT,
            cidr_blocks=["0.0.0.0/0"],
            description=(
                "Allow traffic to the odl-video-service server on port"
                f" {DEFAULT_HTTP_PORT}"
            ),
        ),
    ],
    egress=default_egress_args,
    vpc_id=target_vpc_id,
    tags=aws_config.tags,
)

ovs_database_security_group = ec2.SecurityGroup(
    f"odl-video-service-database-security-group-{env_name}",
    name=f"odl-video-service-database-{target_vpc_name}-{env_name}",
    description="Access control for the odl-video-service database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                ovs_server_security_group.id,
                consul_stack.require_output("security_groups")["consul_server"],
                vault_stack.require_output("vault_server")["security_group"],
                data_vpc["security_groups"]["integrator"],
            ],
            cidr_blocks=data_vpc["k8s_pod_subnet_cidrs"].apply(
                lambda pod_cidrs: [*pod_cidrs, target_vpc["cidr"]]
            ),
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description=(
                "Access to Postgres from odl-video-service nodes on"
                f" {DEFAULT_POSTGRES_PORT}"
            ),
        ),
    ],
    vpc_id=target_vpc_id,
    tags=aws_config.tags,
)

ovs_redis_security_group = ec2.SecurityGroup(
    f"odl-video-service-redis-security-group-{env_name}",
    name=f"odl-video-service-redis-{target_vpc_name}-{env_name}",
    description="Access control for the odl-video-service redis queue",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                ovs_server_security_group.id,
            ],
            protocol="tcp",
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            description=(
                f"Access to Redis from odl-video-service nodes on {DEFAULT_REDIS_PORT}"
            ),
        ),
    ],
    egress=default_egress_args,
    vpc_id=target_vpc_id,
    tags=aws_config.tags,
)

# Database
rds_defaults = defaults(stack_info)["rds"]

rds_password = ovs_config.require("rds_password")

ovs_db_config = OLPostgresDBConfig(
    instance_name=f"odl-video-service-{stack_info.env_suffix}",
    password=rds_password,
    storage=ovs_config.get("db_capacity") or str(AWS_RDS_DEFAULT_DATABASE_CAPACITY),
    subnet_group_name=target_vpc["rds_subnet"],
    security_groups=[ovs_database_security_group],
    engine_major_version="18",
    parameter_overrides=[],
    db_name="odlvideo",
    tags=aws_config.tags,
    **rds_defaults,
)
ovs_db = OLAmazonDB(ovs_db_config)

db_address = ovs_db.db_instance.address
db_port = ovs_db.db_instance.port

ovs_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=ovs_db_config.db_name,
    mount_point=f"{ovs_db_config.engine}-odl-video-service",
    db_admin_username=ovs_db_config.username,
    db_admin_password=rds_password,
    db_host=db_address,
)
ovs_db_vault_backend = OLVaultDatabaseBackend(ovs_db_vault_backend_config)

ovs_db_consul_node = Node(
    f"odl-video-service-{stack_info.env_suffix}-db-node",
    name="ovs-postgres-db",
    address=db_address,
    opts=consul_provider,
)

ovs_db_consul_service = Service(
    f"odl-video-service-{stack_info.env_suffix}-db-service",
    node=ovs_db_consul_node.name,
    name="ovs-postgres",
    port=db_port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="ovs-instance-db",
            interval="10s",
            name="ovs-instance-db",
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

redis_auth_token = secrets["redis"]["auth_token"]

ovs_server_redis_config = OLAmazonRedisConfig(
    encrypt_transit=True,
    auth_token=redis_auth_token,
    engine_version="7.2",
    engine="valkey",
    num_instances=3,
    shard_count=1,
    auto_upgrade=True,
    cluster_mode_enabled=False,
    cluster_description="Redis cluster for ODL Video Service.",
    cluster_name=f"odl-video-service-redis-{stack_info.env_suffix}",
    security_groups=[ovs_redis_security_group.id],
    subnet_group=target_vpc["elasticache_subnet"],
    tags=aws_config.tags,
    **defaults(stack_info)["redis"],
)
ovs_server_redis_cluster = OLAmazonCache(
    ovs_server_redis_config,
    opts=ResourceOptions(
        aliases=[
            Alias(
                name=f"odl-video-service-redis-{stack_info.env_suffix}-redis-elasticache-cluster"
            )
        ]
    ),
)

# Provision EC2 resources
instance_type_name = (
    ovs_config.get("instance_type") or InstanceTypes.burstable_medium.name
)
instance_type = InstanceTypes[instance_type_name].value

subnets = target_vpc["subnet_ids"]
subnet_id = subnets.apply(chain)

grafana_credentials = read_yaml_secrets(
    Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
)
consul_datacenter = consul_stack.require_output("datacenter")

instance_tags = aws_config.merged_tags(
    {"Name": f"odl-video-service-{stack_info.env_suffix}"}
)

branch_tag = ovs_config.get("ami_branch_tag") or "master"

ovs_server_ami = ec2.get_ami(
    filters=[
        {
            "name": "tag:Name",
            "values": ["odl_video_service-server"],
        },
        {
            "name": "tag:branch",
            "values": [branch_tag],
        },
        {
            "name": "virtualization-type",
            "values": ["hvm"],
        },
    ],
    most_recent=True,
    owners=[str(aws_account.id)],
)

block_device_mappings = [BlockDeviceMapping()]

ovs_lb_config = OLLoadBalancerConfig(
    enable_insecure_http=True,
    listener_cert_domain="*.odl.mit.edu",
    listener_use_acm=True,
    security_groups=[ovs_server_security_group],
    subnets=subnets,
    tags=instance_tags,
)

ovs_tg_config = OLTargetGroupConfig(
    vpc_id=target_vpc["id"],
    target_group_healthcheck=False,
    health_check_interval=60,
    health_check_matcher="404",  # TODO Figure out a real endpoint  # noqa: FIX002, TD002, TD004
    health_check_path="/ping",
    stickiness="lb_cookie",
    tags=instance_tags,
)

ovs_lt_config = OLLaunchTemplateConfig(
    block_device_mappings=block_device_mappings,
    image_id=ovs_server_ami.id,
    instance_type=instance_type,
    instance_profile_arn=ovs_server_instance_profile.arn,
    security_groups=[
        target_vpc["security_groups"]["default"],
        ovs_server_security_group,
    ],
    tags=instance_tags,
    tag_specifications=[
        TagSpecification(
            resource_type="instance",
            tags=instance_tags,
        ),
        TagSpecification(
            resource_type="volume",
            tags=instance_tags,
        ),
    ],
    user_data=consul_datacenter.apply(
        lambda consul_dc: base64.b64encode(
            "#cloud-config\n{}".format(
                yaml.dump(
                    {
                        "write_files": [
                            {
                                "path": "/etc/consul.d/02-autojoin.json",
                                "content": json.dumps(
                                    {
                                        "retry_join": [
                                            "provider=aws tag_key=consul_env "
                                            f"tag_value={consul_dc}"
                                        ],
                                        "datacenter": consul_dc,
                                    }
                                ),
                                "owner": "consul:consul",
                            },
                            {
                                "path": "/etc/default/vector",
                                "content": textwrap.dedent(
                                    f"""\
                            ENVIRONMENT={consul_dc}
                            APPLICATION=ovs
                            SERVICE=ovs
                            VECTOR_CONFIG_DIR=/etc/vector/
                            VECTOR_STRICT_ENV_VARS=false
                            AWS_REGION={aws_config.region}
                            GRAFANA_CLOUD_API_KEY={grafana_credentials["api_key"]}
                            GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials["prometheus_user_id"]}
                            GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials["loki_user_id"]}
                            """
                                ),
                                "owner": "root:root",
                            },
                        ],
                    }
                ),
            ).encode("utf8")
        ).decode("utf8")
    ),
)

ovs_autoscale_sizes = ovs_config.get_object("auto_scale") or {
    "desired": 2,
    "min": 1,
    "max": 3,
}
ovs_asg_config = OLAutoScaleGroupConfig(
    asg_name=f"odl-video-service-{stack_info.env_suffix}",
    aws_config=aws_config,
    desired_size=ovs_autoscale_sizes["desired"],
    min_size=ovs_autoscale_sizes["min"],
    max_size=ovs_autoscale_sizes["max"],
    vpc_zone_identifiers=target_vpc["subnet_ids"],
    tags=instance_tags,
)

autoscale_setup = OLAutoScaling(
    asg_config=ovs_asg_config,
    lt_config=ovs_lt_config,
    tg_config=ovs_tg_config,
    lb_config=ovs_lb_config,
)

# Configuration variables needed for both EC2 and K8s deployments
enabled_annotations = ovs_config.get_bool("feature_annotations")
use_shibboleth = ovs_config.get_bool("use_shibboleth")

# MediaConvert setup (shared by both EC2 and K8s)
ovs_mediaconvert_config = MediaConvertConfig(
    service_name="odl-video",
    env_suffix=stack_info.env_suffix,
    tags=aws_config.tags,
    policy_arn=ovs_server_policy.arn,
    host=ovs_config.get("default_domain"),
)
ovs_mediaconvert = OLMediaConvert(ovs_mediaconvert_config)

# Kubernetes Deployment (optional, based on configuration)
deploy_to_k8s = ovs_config.get_bool("deploy_to_k8s")

if deploy_to_k8s:
    # Setup K8s provider and get cluster information
    k8s_cluster_name = "applications"
    k8s_namespace = "odl-video-service"

    eks_stack = StackReference(
        f"infrastructure.aws.eks.{k8s_cluster_name}.{stack_info.name}"
    )

    k8s_provider = setup_k8s_provider(
        kubeconfig=eks_stack.require_output("kube_config")
    )

    eks_stack.require_output("namespaces").apply(
        lambda ns: check_cluster_namespace(k8s_namespace, ns)
    )

    # Create K8s labels
    k8s_app_labels = K8sAppLabels(
        application=Application.odl_video_service,
        stack=stack_info,
        ou=BusinessUnit.ovs,
        service=Services.odl_video_service,
        product=Product.infrastructure,
        source_repository="https://github.com/mitodl/odl-video-service",
    )

    # Read and prepare Vault policy text
    vault_policy_text = (
        Path(__file__)
        .parent.joinpath("odl_video_service_server_policy.hcl")
        .read_text()
    )

    # Setup IAM and Vault auth using OLEKSAuthBinding
    ovs_auth_binding = OLEKSAuthBinding(
        OLEKSAuthBindingConfig(
            application_name=f"odl-video-service-{stack_info.env_suffix}",
            namespace=k8s_namespace,
            stack_info=stack_info,
            aws_config=aws_config,
            iam_policy_document=ovs_server_policy_document,
            vault_policy_text=vault_policy_text,
            cluster_name=eks_stack.require_output("cluster_name"),
            cluster_identities=eks_stack.require_output("cluster_identities"),
            vault_auth_endpoint=eks_stack.require_output("vault_auth_endpoint"),
            irsa_service_account_name="odl-video-service",
            vault_sync_service_account_names="odl-video-service-vault",
            k8s_labels=k8s_app_labels,
            parliament_config=parliament_config,
        )
    )

    # Get vault_k8s_resources from the auth binding
    vault_k8s_resources = ovs_auth_binding.vault_k8s_resources

    # Create VaultDynamicSecret for database credentials
    db_creds_secret = OLVaultK8SSecret(
        f"odl-video-service-{stack_info.env_suffix}-db-creds",
        resource_config=OLVaultK8SDynamicSecretConfig(
            name="odl-video-db-creds",
            namespace=k8s_namespace,
            labels=k8s_app_labels.model_dump(),
            dest_secret_name="odl-video-db-creds",  # noqa: S106 # pragma: allowlist secret
            dest_secret_labels=k8s_app_labels.model_dump(),
            mount=f"postgres-{stack_info.env_prefix}",
            path="creds/ovs-app",
            excludes=[".*"],
            exclude_raw=True,
            templates={
                "DB_USER": "{{ .Secrets.username }}",
                "DB_PASSWORD": "{{ .Secrets.password }}",
            },
            vaultauth=vault_k8s_resources.auth_name,
            restart_target_kind="Deployment",
            restart_target_name="odl-video-service",
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=vault_k8s_resources,
        ),
    )

    # Create VaultDynamicSecret for AWS credentials
    aws_creds_secret = OLVaultK8SSecret(
        f"odl-video-service-{stack_info.env_suffix}-aws-creds",
        resource_config=OLVaultK8SDynamicSecretConfig(
            name="odl-video-aws-creds",
            namespace=k8s_namespace,
            labels=k8s_app_labels.model_dump(),
            dest_secret_name="odl-video-aws-creds",  # noqa: S106 # pragma: allowlist secret
            dest_secret_labels=k8s_app_labels.model_dump(),
            mount="aws-mitx",
            path="creds/ovs-server",
            excludes=[".*"],
            exclude_raw=True,
            templates={
                "AWS_ACCESS_KEY_ID": "{{ .Secrets.access_key }}",
                "AWS_SECRET_ACCESS_KEY": "{{ .Secrets.secret_key }}",
            },
            vaultauth=vault_k8s_resources.auth_name,
            restart_target_kind="Deployment",
            restart_target_name="odl-video-service",
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=vault_k8s_resources,
        ),
    )

    # Create VaultStaticSecret for global CloudFront key
    cloudfront_secret = OLVaultK8SSecret(
        f"odl-video-service-{stack_info.env_suffix}-cloudfront",
        resource_config=OLVaultK8SStaticSecretConfig(
            name="odl-video-cloudfront",
            namespace=k8s_namespace,
            labels=k8s_app_labels.model_dump(),
            dest_secret_name="odl-video-cloudfront",  # noqa: S106 # pragma: allowlist secret
            dest_secret_labels=k8s_app_labels.model_dump(),
            mount="secret-operations",
            mount_type="kv-v2",
            path="global/cloudfront-private-key",
            excludes=[".*"],
            exclude_raw=True,
            templates={
                "CLOUDFRONT_KEY_ID": '{{ index .Secrets "data" "id" }}',
                "CLOUDFRONT_PRIVATE_KEY": '{{ index .Secrets "data" "value" }}',
            },
            vaultauth=vault_k8s_resources.auth_name,
            refresh_after="1h",
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=vault_k8s_resources,
        ),
    )

    # Create VaultStaticSecret for global Mailgun key
    mailgun_secret = OLVaultK8SSecret(
        f"odl-video-service-{stack_info.env_suffix}-mailgun",
        resource_config=OLVaultK8SStaticSecretConfig(
            name="odl-video-mailgun",
            namespace=k8s_namespace,
            labels=k8s_app_labels.model_dump(),
            dest_secret_name="odl-video-mailgun",  # noqa: S106 # pragma: allowlist secret
            dest_secret_labels=k8s_app_labels.model_dump(),
            mount="secret-operations",
            mount_type="kv-v2",
            path="global/mailgun-api-key",
            excludes=[".*"],
            exclude_raw=True,
            templates={
                "MAILGUN_KEY": '{{ index .Secrets "data" "value" }}',
            },
            vaultauth=vault_k8s_resources.auth_name,
            refresh_after="1h",
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=vault_k8s_resources,
        ),
    )

    # Create VaultStaticSecret for application secrets
    app_static_secret = OLVaultK8SSecret(
        f"odl-video-service-{stack_info.env_suffix}-static-secret",
        resource_config=OLVaultK8SStaticSecretConfig(
            name="odl-video-static-secrets",
            namespace=k8s_namespace,
            labels=k8s_app_labels.model_dump(),
            dest_secret_name="odl-video-static-secrets",  # noqa: S106 # pragma: allowlist secret
            dest_secret_labels=k8s_app_labels.model_dump(),
            mount="secret-odl-video-service",
            mount_type="kv-v2",
            path="ovs-secrets",
            excludes=[".*"],
            exclude_raw=True,
            templates={
                # Django secrets
                "SECRET_KEY": '{{ index .Secrets "data" "misc" "secret_key" }}',
                "FIELD_ENCRYPTION_KEY": (
                    '{{ index .Secrets "data" "misc" "field_encryption_key" }}'
                ),
                # MIT Web Services
                "MIT_WS_CERTIFICATE": (
                    '{{ index .Secrets "data" "misc" "mit_ws_certificate" }}'
                ),
                "MIT_WS_PRIVATE_KEY": (
                    '{{ index .Secrets "data" "misc" "mit_ws_private_key" }}'
                ),
                "ET_PIPELINE_ID": '{{ index .Secrets "data" "misc" "et_pipeline_id" }}',
                # Mailgun
                "MAILGUN_URL": '{{ index .Secrets "data" "mailgun" "url" }}',
                # OpenEdX
                "OPENEDX_API_CLIENT_ID": (
                    '{{ index .Secrets "data" "openedx" "api_client_id" }}'
                ),
                "OPENEDX_API_CLIENT_SECRET": (
                    '{{ index .Secrets "data" "openedx" "api_client_secret" }}'
                ),
                # Sentry
                "SENTRY_DSN": '{{ index .Secrets "data" "sentry" "dsn" }}',
                # Google Analytics (note: using GA_ prefix to match .env.tmpl)
                "GA_VIEW_ID": '{{ index .Secrets "data" "google_analytics" "id" }}',
                "GA_KEYFILE_JSON": (
                    '{{ index .Secrets "data" "google_analytics" "json" }}'
                ),
                "GA_TRACKING_ID": (
                    '{{ index .Secrets "data" "google_analytics" "tracking_id" }}'
                ),
                # YouTube (note: using YT_ prefix to match .env.tmpl)
                "YT_ACCESS_TOKEN": (
                    '{{ index .Secrets "data" "youtube" "access_token" }}'
                ),
                "YT_CLIENT_ID": '{{ index .Secrets "data" "youtube" "client_id" }}',
                "YT_CLIENT_SECRET": (
                    '{{ index .Secrets "data" "youtube" "client_secret" }}'
                ),
                "YT_PROJECT_ID": '{{ index .Secrets "data" "youtube" "project_id" }}',
                "YT_REFRESH_TOKEN": (
                    '{{ index .Secrets "data" "youtube" "refresh_token" }}'
                ),
                # Dropbox
                "DROPBOX_KEY": '{{ index .Secrets "data" "dropbox" "key" }}',
                "DROPBOX_TOKEN": '{{ index .Secrets "data" "dropbox" "token" }}',
                # CloudFront
                "VIDEO_CLOUDFRONT_DIST": (
                    '{{ index .Secrets "data" "cloudfront" "subdomain" }}'
                ),
                # Redis (note: auth_token is used for CELERY_BROKER_URL/REDIS_URL construction)
                "REDIS_AUTH_TOKEN": '{{ index .Secrets "data" "redis" "auth_token" }}',
            },
            vaultauth=vault_k8s_resources.auth_name,
            refresh_after="1h",
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=vault_k8s_resources,
        ),
    )

    # Create Shibboleth ConfigMap (if Shibboleth is enabled)
    if use_shibboleth:
        shibboleth_config = kubernetes.core.v1.ConfigMap(
            "odl-video-shibboleth-config",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name="odl-video-shibboleth-config",
                namespace=k8s_namespace,
                labels=k8s_app_labels.model_dump(),
            ),
            data={
                # ruff: noqa: E501
                "shibboleth2.xml": textwrap.dedent(f"""\
                    <SPConfig xmlns="urn:mace:shibboleth:3.0:native:sp:config"
                              xmlns:conf="urn:mace:shibboleth:3.0:native:sp:config">
                        <ApplicationDefaults entityID="https://{ovs_config.get("default_domain")}/shibboleth">
                            <Sessions lifetime="28800" timeout="3600" relayState="ss:mem"
                                      checkAddress="false" handlerSSL="true" cookieProps="https">
                                <SSO entityID="https://idp.mit.edu/shibboleth">
                                    SAML2
                                </SSO>
                                <Logout>SAML2 Local</Logout>
                                <Handler type="MetadataGenerator" Location="/Metadata" signing="false"/>
                                <Handler type="Status" Location="/Status" acl="127.0.0.1 ::1"/>
                                <Handler type="Session" Location="/Session" showAttributeValues="false"/>
                            </Sessions>
                            <Errors supportContact="mitx-devops@mit.edu"
                                    helpLocation="/about.html"
                                    styleSheet="/shibboleth-sp/main.css"/>
                            <MetadataProvider type="XML" validate="true"
                                              path="/etc/shibboleth/mit-metadata.xml"/>
                            <AttributeExtractor type="XML" validate="true" reloadChanges="false"
                                                path="/etc/shibboleth/attribute-map.xml"/>
                            <AttributeResolver type="Query" subjectMatch="true"/>
                            <AttributeFilter type="XML" validate="true"
                                             path="/etc/shibboleth/attribute-policy.xml"/>
                            <CredentialResolver type="File" use="signing"
                                                key="/etc/shibboleth/sp-key.pem"
                                                certificate="/etc/shibboleth/sp-cert.pem"/>
                            <CredentialResolver type="File" use="encryption"
                                                key="/etc/shibboleth/sp-key.pem"
                                                certificate="/etc/shibboleth/sp-cert.pem"/>
                        </ApplicationDefaults>
                        <SecurityPolicyProvider type="XML" validate="true" path="/etc/shibboleth/security-policy.xml"/>
                        <ProtocolProvider type="XML" validate="true" reloadChanges="false" path="/etc/shibboleth/protocols.xml"/>
                    </SPConfig>
                    """),
                "attribute-map.xml": textwrap.dedent("""\
                    <Attributes xmlns="urn:mace:shibboleth:2.0:attribute-map" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
                        <Attribute name="urn:oid:1.3.6.1.4.1.5923.1.1.1.6" id="eppn">
                            <AttributeDecoder xsi:type="ScopedAttributeDecoder" caseSensitive="false"/>
                        </Attribute>
                        <Attribute name="urn:oid:1.3.6.1.4.1.5923.1.1.1.9" id="affiliation">
                            <AttributeDecoder xsi:type="ScopedAttributeDecoder" caseSensitive="false"/>
                        </Attribute>
                        <Attribute name="urn:oid:2.5.4.3" id="cn"/>
                        <Attribute name="urn:oid:0.9.2342.19200300.100.1.1" id="uid"/>
                        <Attribute name="urn:oid:2.5.4.42" id="givenName"/>
                        <Attribute name="urn:oid:2.5.4.4" id="sn"/>
                        <Attribute name="urn:oid:0.9.2342.19200300.100.1.3" id="mail"/>
                    </Attributes>
                    """),
            },
            opts=ResourceOptions(
                provider=k8s_provider,
                parent=vault_k8s_resources,
            ),
        )

        # Create Shibboleth Secrets (certs and keys)
        shibboleth_certs_secret = kubernetes.core.v1.Secret(
            "odl-video-shibboleth-certs",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name="odl-video-shibboleth-certs",
                namespace=k8s_namespace,
                labels=k8s_app_labels.model_dump(),
            ),
            string_data={
                "sp-cert.pem": secrets["shibboleth"]["sp_cert"],
                "sp-key.pem": secrets["shibboleth"]["sp_key"],
                "mit-md-cert.pem": secrets["shibboleth"]["mit_md_cert"],
            },
            opts=ResourceOptions(
                provider=k8s_provider,
                parent=vault_k8s_resources,
            ),
        )

    # Prepare environment variables for the application
    ovs_env_vars = Output.all(
        db_address=db_address,
        redis_address=ovs_server_redis_cluster.address,
        redis_auth_token=redis_auth_token,
        mediaconvert_sns_topic=ovs_mediaconvert.sns_topic.arn,
        mediaconvert_queue=ovs_mediaconvert.queue.name,
        mediaconvert_role=ovs_mediaconvert.role.name,
    ).apply(
        lambda args: {
            # Database connection (credentials from VaultDynamicSecret)
            "DATABASE_HOST": args["db_address"],
            "DATABASE_PORT": str(DEFAULT_POSTGRES_PORT),
            "DATABASE_NAME": "odlvideo",
            # Redis/Celery configuration (using rediss:// for TLS)
            "CELERY_BROKER_URL": f"rediss://default:{args['redis_auth_token']}@{args['redis_address']}:6379/0?ssl_cert_reqs=required",
            "REDIS_URL": f"rediss://default:{args['redis_auth_token']}@{args['redis_address']}:6379/0?ssl_cert_reqs=CERT_REQUIRED",
            # Application configuration
            "DJANGO_LOG_LEVEL": ovs_config.get("log_level") or "INFO",
            "ODL_VIDEO_LOG_LEVEL": ovs_config.get("log_level") or "INFO",
            "ODL_VIDEO_ENVIRONMENT": stack_info.env_suffix,
            "ODL_VIDEO_BASE_URL": f"https://{ovs_config.get('default_domain')}",
            "EDX_BASE_URL": ovs_config.get("edx_base_url"),
            "NGINX_CONFIG_FILE_PATH": ovs_config.get("nginx_config_file_path")
            or "/etc/nginx/conf.d/odl-video.conf",
            "STATUS_TOKEN": stack_info.env_suffix,
            # AWS configuration
            "AWS_REGION": aws_config.region,
            "AWS_ACCOUNT_ID": aws_account.account_id,
            "AWS_ROLE_NAME": args["mediaconvert_role"],
            "AWS_S3_DOMAIN": "s3.amazonaws.com",
            # S3 bucket configuration (using VIDEO_S3_* to match .env.tmpl)
            "VIDEO_S3_BUCKET": ovs_config.get("s3_bucket_name"),
            "VIDEO_S3_SUBTITLE_BUCKET": ovs_config.get("s3_subtitle_bucket_name"),
            "VIDEO_S3_THUMBNAIL_BUCKET": ovs_config.get("s3_thumbnail_bucket_name"),
            "VIDEO_S3_TRANSCODE_BUCKET": ovs_config.get("s3_transcode_bucket_name"),
            "VIDEO_S3_WATCH_BUCKET": ovs_config.get("s3_watch_bucket_name"),
            "VIDEO_S3_UPLOAD_PREFIX": ovs_config.get("video_s3_upload_prefix") or "",
            "VIDEO_S3_TRANSCODE_PREFIX": ovs_config.get("video_s3_transcode_prefix")
            or "transcoded",
            "VIDEO_S3_THUMBNAIL_PREFIX": ovs_config.get("video_s3_thumbnail_prefix")
            or "thumbnails",
            "VIDEO_S3_TRANSCODE_ENDPOINT": ovs_config.get(
                "video_s3_transcode_endpoint"
            ),
            # MediaConvert/Transcode configuration
            "POST_TRANSCODE_ACTIONS": ovs_config.get("post_transcode_actions")
            or "cloudsync_api_process_transcode_results",
            "TRANSCODE_JOB_TEMPLATE": ovs_config.get("transcode_job_template")
            or "./config/mediaconvert.json",
            "VIDEO_TRANSCODE_QUEUE": args["mediaconvert_queue"],
            # Elastic Transcoder legacy presets
            "ET_MP4_PRESET_ID": "1669811490975-riqq25",
            "ET_PRESET_IDS": "1504127981921-c2jlwt,1504127981867-06dkm6,1504127981819-v44xlx,1504127981769-6cnqhq,1351620000001-200040,1351620000001-200050",
            # Redis configuration
            "REDIS_MAX_CONNECTIONS": ovs_config.get("redis_max_connections") or "10",
            # Feature flags
            "USE_SHIBBOLETH": "True" if use_shibboleth else "False",
            "FEATURE_VIDEOJS_ANNOTATIONS": "True" if enabled_annotations else "False",
            "FEATURE_RETRANSCODE_ENABLED": "True",
            "ENABLE_VIDEO_PERMISSIONS": "True",
            # Video processing configuration
            "VIDEO_STATUS_UPDATE_FREQUENCY": "60",
            "VIDEO_WATCH_BUCKET_FREQUENCY": "600",
            # Dropbox configuration
            "DROPBOX_FOLDER": "/Captions",
            # Google Analytics configuration
            "GA_DIMENSION_CAMERA": "dimension1",
            # Email configuration
            "LECTURE_CAPTURE_USER": "emello@mit.edu",
            "ODL_VIDEO_ADMIN_EMAIL": "cuddle_bunnies@mit.edu",
            "ODL_VIDEO_FROM_EMAIL": "MIT ODL Video <ol-engineering-support@mit.edu>",
            "ODL_VIDEO_SUPPORT_EMAIL": "MIT ODL Video <ol-engineering-support@mit.edu>",
            "ODL_VIDEO_LOG_FILE": "/var/log/odl-video/django.log",
            # Application runtime configuration
            "PORT": "8087",
            "NODE_ENV": "production",
            "DEV_ENV": "False",
        }
    )

    # Create main application configuration Secret
    ovs_k8s_config_secret = kubernetes.core.v1.Secret(
        "odl-video-service-config",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="odl-video-config",
            namespace=k8s_namespace,
            labels=k8s_app_labels.model_dump(),
        ),
        string_data=ovs_env_vars,
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=vault_k8s_resources,
        ),
    )

    # Collect all secret names for env injection
    secret_names = [
        "odl-video-config",
        "odl-video-db-creds",
        "odl-video-aws-creds",
        "odl-video-cloudfront",
        "odl-video-mailgun",
        "odl-video-static-secrets",
    ]

    # Create K8s-safe security group name (replace underscores with hyphens)
    k8s_safe_sg_name = ovs_server_security_group.name.apply(
        lambda name: name.replace("_", "-")
    )

    # Configure OLApplicationK8s
    ovs_k8s_config = OLApplicationK8sConfig(
        project_root=Path(__file__).parent,
        application_config={},  # Use env_from_secret_names instead
        application_name="odl-video-service",
        application_namespace=k8s_namespace,
        application_lb_service_name="odl-video-service",
        application_lb_service_port_name="http",
        application_min_replicas=ovs_config.get_int("web_min_replicas") or 2,
        application_max_replicas=ovs_config.get_int("web_max_replicas") or 10,
        k8s_global_labels=k8s_app_labels.model_dump(),
        env_from_secret_names=secret_names,
        application_security_group_id=ovs_server_security_group.id,
        application_security_group_name=k8s_safe_sg_name,
        application_service_account_name=vault_k8s_resources.service_account_name,
        application_image_repository="dockerhub/mitodl/ovs-app",  # From docker-compose.yaml.tmpl
        application_docker_tag=ODL_VIDEO_DOCKER_TAG,  # From environment variable
        application_cmd_array=["uwsgi"],
        application_arg_array=["uwsgi.ini"],
        vault_k8s_resource_auth_name=vault_k8s_resources.auth_name,
        registry="ecr",
        import_nginx_config=True,
        import_uwsgi_config=False,  # uwsgi.ini is already in the Docker image
        init_migrations=True,
        init_collectstatic=True,
        resource_requests={
            "cpu": ovs_config.get("web_cpu_request") or "500m",
            "memory": ovs_config.get("web_memory_request") or "1Gi",
        },
        resource_limits={
            "memory": ovs_config.get("web_memory_limit") or "4Gi",
        },
        celery_worker_configs=[
            OLApplicationK8sCeleryWorkerConfig(
                queue_name="default",
                queues=["default"],
                min_replicas=ovs_config.get_int("celery_min_replicas") or 1,
                max_replicas=ovs_config.get_int("celery_max_replicas") or 5,
                redis_host=ovs_server_redis_cluster.address,
                redis_password=redis_auth_token,
                resource_requests={
                    "cpu": ovs_config.get("celery_cpu_request") or "250m",
                    "memory": ovs_config.get("celery_memory_request") or "512Mi",
                },
                resource_limits={
                    "memory": ovs_config.get("celery_memory_limit") or "2Gi",
                },
            ),
        ],
    )

    # Deploy the application to K8s
    depends_on_list = [
        vault_k8s_resources,
        db_creds_secret,
        app_static_secret,
        ovs_k8s_config_secret,
    ]
    if use_shibboleth:
        depends_on_list.extend([shibboleth_config, shibboleth_certs_secret])

    ovs_k8s_app = OLApplicationK8s(
        ol_app_k8s_config=ovs_k8s_config,
        opts=ResourceOptions(
            provider=k8s_provider,
            depends_on=depends_on_list,
        ),
    )

    export(
        "odl_video_service_k8s",
        {
            "namespace": k8s_namespace,
            "service_name": "odl-video-service",
            "vault_auth_role": ovs_auth_binding.vault_k8s_resources.auth_name,
            "irsa_role_arn": ovs_auth_binding.irsa_role.arn,
        },
    )

# Vault policy definition for EC2 instances
vault_policy_template_text = (
    Path(__file__).parent.joinpath("odl_video_service_server_policy.hcl").read_text()
)
ec2_vault_policy_text = vault_policy_template_text.replace(
    "DEPLOYMENT", stack_info.env_prefix
)
ovs_server_vault_policy = vault.Policy(
    "ovs-server-vault-policy",
    name="odl-video-service-server",
    policy=ec2_vault_policy_text,
)

vault.aws.AuthBackendRole(
    "odl-video-service-server-ec2-vault-auth",
    backend="aws",
    auth_type="iam",
    role="ovs-server",
    inferred_entity_type="ec2_instance",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[ovs_server_instance_profile.arn],
    bound_ami_ids=[ovs_server_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[target_vpc_id],
    token_policies=[ovs_server_vault_policy.name],
)

# Vault KV2 mount definition
ovs_server_vault_mount = vault.Mount(
    "ovs-server-configuration-secrets-mount",
    path="secret-odl-video-service",
    type="kv-v2",
    options={"version": 2},
    description=(
        "Storage of configuration credentials and secrets used by odl-video-service"
    ),
    opts=ResourceOptions(delete_before_replace=True),
)


ovs_server_secrets = vault.generic.Secret(
    "ovs-server-configuration-secrets",
    path=ovs_server_vault_mount.path.apply("{}/ovs-secrets".format),
    data_json=json.dumps(secrets),
)

# These variables are already defined earlier, before K8s deployment
if use_shibboleth:
    nginx_config_file_path = "/etc/nginx/nginx_with_shib.conf"
else:
    nginx_config_file_path = "/etc/nginx/nginx_wo_shib.conf"

domains_string = ",".join(ovs_config.get_object("domains"))

# Create Route53 DNS records
five_minutes = 60 * 5
for domain in ovs_config.get_object("route53_managed_domains"):
    route53.Record(
        f"ovs-server-dns-record-{domain}",
        name=domain,
        type="CNAME",
        ttl=five_minutes,
        records=[autoscale_setup.load_balancer.dns_name],
        zone_id=mitodl_zone_id,
    )

consul_keys = {
    "ovs/database_endpoint": db_address,
    "ovs/default_domain": ovs_config.get("default_domain"),
    "ovs/domains": domains_string,
    "ovs/edx_base_url": ovs_config.get("edx_base_url"),
    "ovs/environment": stack_info.env_suffix,
    "ovs/feature_annotations": ("True" if enabled_annotations else "False"),
    "ovs/log_level": ovs_config.get("log_level"),
    "ovs/mediaconvert_sns_topic_arn": ovs_mediaconvert.sns_topic.arn,
    "ovs/nginx_config_file_path": nginx_config_file_path,
    "ovs/redis_cluster_address": ovs_server_redis_cluster.address,
    "ovs/redis_max_connections": redis_config.get("max_connections") or 65000,
    "ovs/s3_bucket_name": ovs_config.get("s3_bucket_name"),
    "ovs/s3_subtitle_bucket_name": ovs_config.get("s3_subtitle_bucket_name"),
    "ovs/s3_thumbnail_bucket_name": ovs_config.get("s3_thumbnail_bucket_name"),
    "ovs/s3_transcode_bucket_name": ovs_config.get("s3_transcode_bucket_name"),
    "ovs/s3_watch_bucket_name": ovs_config.get("s3_watch_bucket_name"),
    "ovs/use_shibboleth": "True" if use_shibboleth else "False",
    "ovs/aws_role_name": ovs_mediaconvert.role.name,
    "ovs/aws_account_id": aws_account.account_id,
    "ovs/post_transcode_actions": "cloudsync.api.process_transcode_results",
    "ovs/transcode_job_template": "./config/mediaconvert.json",
    "ovs/video_s3_thumbnail_bucket": ovs_config.get("s3_thumbnail_bucket_name"),
    "ovs/video_s3_transcode_bucket": ovs_config.get("s3_transcode_bucket_name"),
    "ovs/video_s3_transcode_endpoint": secrets["transcode_endpoint"],
    "ovs/video_s3_upload_prefix": "",
    "ovs/video_s3_transcode_prefix": "transcoded",
    "ovs/video_s3_thumbnail_prefix": "thumbnails",
    "ovs/video_transcode_queue": ovs_mediaconvert.queue.name,
}
consul.Keys(
    "ovs-server-configuration-data",
    keys=consul_key_helper(consul_keys),
    opts=consul_provider,
)

# Add the resources to the export
export(
    "odl_video_service",
    {
        "rds_host": db_address,
        "redis_cluster": ovs_server_redis_cluster.address,
        "mediaconvert_queue": ovs_mediaconvert.queue.id,
    },
)
