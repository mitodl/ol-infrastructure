"""The complete state needed to provision OVS running on Docker."""

import base64
import json
import textwrap
from itertools import chain
from pathlib import Path

import pulumi_consul as consul
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
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import InstanceTypes, default_egress_args
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.consul import consul_key_helper, get_consul_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

# Configuration items and initialziations
if Config("vault_server").get("env_namespace"):
    setup_vault_provider()
ovs_config = Config("ovs")
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
redis_config = Config("redis")

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
    health_check_matcher="404",  # TODO Figure out a real endpoint for this  # noqa: E501, FIX002, TD002, TD004
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

# Vault policy definition
ovs_server_vault_policy = vault.Policy(
    "ovs-server-vault-policy",
    name="odl-video-service-server",
    policy=Path(__file__)
    .parent.joinpath("odl_video_service_server_policy.hcl")
    .read_text(),
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
enabled_annotations = ovs_config.get_bool("feature_annotations")
use_shibboleth = ovs_config.get_bool("use_shibboleth")
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

ovs_mediaconvert_config = MediaConvertConfig(
    service_name="odl-video",
    env_suffix=stack_info.env_suffix,
    tags=aws_config.tags,
    policy_arn=ovs_server_policy.arn,
    host=ovs_config.get("default_domain"),
)

ovs_mediaconvert = OLMediaConvert(ovs_mediaconvert_config)

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
