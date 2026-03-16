"""The complete state needed to provision OVS running on Docker.

- Create S3 buckets for video storage
- Create a PostgreSQL database in AWS RDS
- Create a Redis cluster in AWS Elasticache
- Create an IAM policy to grant access to S3 and other resources
- Create MediaConvert resources for video transcoding
"""

import json
import os
from pathlib import Path

import pulumi_kubernetes as kubernetes
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
from pulumi_aws import ec2, get_caller_identity, iam
from pulumi_aws.s3 import BucketCorsConfigurationCorsRuleArgs

from bridge.lib.magic_numbers import (
    AWS_RDS_DEFAULT_DATABASE_CAPACITY,
    DEFAULT_HTTPS_PORT,
    DEFAULT_POSTGRES_PORT,
    DEFAULT_REDIS_PORT,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.applications.odl_video_service.k8s_secrets import (
    create_ovs_k8s_secrets,
)
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.mediaconvert import (
    MediaConvertConfig,
    OLMediaConvert,
)
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.k8s import (
    OLApisixRoute,
    OLApisixRouteConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import default_egress_args
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    default_psg_egress_args,
    get_default_psg_ingress_args,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
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

# Configuration items and initialziations
if Config("vault_server").get("env_namespace"):
    setup_vault_provider()
ovs_config = Config("ovs")
stack_info = parse_stack()
k8s_cutover = ovs_config.get_bool("k8s_cutover") or False

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
    cors_rules=[
        BucketCorsConfigurationCorsRuleArgs(
            allowed_headers=["*"],
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
            max_age_seconds=3000,
        )
    ],
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
    cors_rules=[
        BucketCorsConfigurationCorsRuleArgs(
            allowed_headers=["*"],
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
            max_age_seconds=3000,
        )
    ],
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
    cors_rules=[
        BucketCorsConfigurationCorsRuleArgs(
            allowed_headers=["*"],
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
            max_age_seconds=3000,
        )
    ],
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
    cors_rules=[
        BucketCorsConfigurationCorsRuleArgs(
            allowed_headers=["*"],
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
            max_age_seconds=3000,
        )
    ],
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
        {
            "Action": ["cloudfront:CreateInvalidation"],
            "Effect": "Allow",
            "Resource": [f"arn:aws:cloudfront::{aws_account.id}:distribution/*"],
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
ovs_database_security_group = ec2.SecurityGroup(
    f"odl-video-service-database-security-group-{env_name}",
    name=f"odl-video-service-database-{target_vpc_name}-{env_name}",
    description="Access control for the odl-video-service database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
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
            cidr_blocks=target_vpc["k8s_pod_subnet_cidrs"],
            protocol="tcp",
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            description=("Allow access from OVS K8s pods to Redis"),
        ),
    ],
    egress=default_egress_args,
    vpc_id=target_vpc_id,
    tags=aws_config.tags,
)

# Database
rds_defaults = defaults(stack_info)["rds"]

rds_password = ovs_config.require("rds_password")

db_instance_name = f"odl-video-service-{stack_info.env_suffix}"
ovs_db_config = OLPostgresDBConfig(
    instance_name=db_instance_name,
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
use_shibboleth = ovs_config.get_bool("use_shibboleth") or False

# MediaConvert resources (needed by both EC2 and K8s paths)
ovs_mediaconvert_config = MediaConvertConfig(
    service_name="odl-video",
    env_suffix=stack_info.env_suffix,
    tags=aws_config.tags,
    policy_arn=ovs_server_policy.arn,
    host=ovs_config.get("default_domain"),
)
ovs_mediaconvert = OLMediaConvert(ovs_mediaconvert_config)

# Non-sensitive env vars shared between EC2 and K8s deployments
app_env_vars: dict[str, str | bool] = {
    "AWS_REGION": "us-east-1",
    "AWS_S3_DOMAIN": "s3.amazonaws.com",
    "DEV_ENV": "False",
    "DJANGO_LOG_LEVEL": ovs_config.get("log_level") or "INFO",
    "DROPBOX_FOLDER": "/Captions",
    "ENABLE_VIDEO_PERMISSIONS": "True",
    "ET_MP4_PRESET_ID": "1669811490975-riqq25",
    "ET_PRESET_IDS": (
        "1504127981921-c2jlwt,1504127981867-06dkm6,"
        "1504127981819-v44xlx,1504127981769-6cnqhq,"
        "1351620000001-200040,1351620000001-200050"
    ),
    "FEATURE_RETRANSCODE_ENABLED": "True",
    "FEATURE_VIDEOJS_ANNOTATIONS": "True" if enabled_annotations else "False",
    "GA_DIMENSION_CAMERA": "dimension1",
    "LECTURE_CAPTURE_USER": "emello@mit.edu",
    "NODE_ENV": "production",
    "ODL_VIDEO_ADMIN_EMAIL": "cuddle_bunnies@mit.edu",
    "ODL_VIDEO_BASE_URL": f"https://{ovs_config.get('default_domain')}",
    "ODL_VIDEO_ENVIRONMENT": stack_info.env_suffix,
    "ODL_VIDEO_FROM_EMAIL": "MIT ODL Video <ol-engineering-support@mit.edu>",
    "ODL_VIDEO_LOG_LEVEL": ovs_config.get("log_level") or "INFO",
    "ODL_VIDEO_SECURE_SSL_REDIRECT": "False",
    "ODL_VIDEO_SUPPORT_EMAIL": "MIT ODL Video <ol-engineering-support@mit.edu>",
    "PORT": "8087",
    "REDIS_MAX_CONNECTIONS": redis_config.get("max_connections") or "65000",
    "STATUS_TOKEN": stack_info.env_suffix,
    "USE_SHIBBOLETH": "True" if use_shibboleth else "False",
    "VIDEO_S3_BUCKET": ovs_config.get("s3_bucket_name") or "",
    "VIDEO_S3_SUBTITLE_BUCKET": ovs_config.get("s3_subtitle_bucket_name") or "",
    "VIDEO_S3_THUMBNAIL_BUCKET": ovs_config.get("s3_thumbnail_bucket_name") or "",
    "VIDEO_S3_THUMBNAIL_PREFIX": "thumbnails",
    "VIDEO_S3_TRANSCODE_BUCKET": ovs_config.get("s3_transcode_bucket_name") or "",
    "VIDEO_S3_TRANSCODE_PREFIX": "transcoded",
    "VIDEO_S3_UPLOAD_PREFIX": "",
    "VIDEO_S3_WATCH_BUCKET": ovs_config.get("s3_watch_bucket_name") or "",
    "VIDEO_STATUS_UPDATE_FREQUENCY": "60",
    "VIDEO_WATCH_BUCKET_FREQUENCY": "600",
}

vault_config = Config("vault")

cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")
k8s_pod_subnet_cidrs = target_vpc["k8s_pod_subnet_cidrs"]

k8s_app_labels = K8sAppLabels(
    application=Application.odl_video_service,
    product=Product.odl_video,
    service=Services.odl_video_service,
    ou=BusinessUnit.ovs,
    source_repository="https://github.com/mitodl/odl-video-service",
    stack=stack_info,
).model_dump()

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))
ovs_namespace = "odl-video-service"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(ovs_namespace, ns)
)

# Pod security group for the OVS application
ovs_app_security_group = ec2.SecurityGroup(
    f"ovs-app-access-{stack_info.env_suffix}",
    description=f"Access control for the OVS App in {stack_info.name}",
    egress=default_psg_egress_args,
    ingress=get_default_psg_ingress_args(k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs),
    tags=aws_config.tags,
    vpc_id=target_vpc_id,
)

# Vault policy and K8s auth
ovs_k8s_vault_policy = vault.Policy(
    f"ovs-k8s-vault-policy-{stack_info.env_suffix}",
    name="odl-video-service-k8s",
    policy=Path(__file__)
    .parent.joinpath("odl_video_service_server_policy.hcl")
    .read_text(),
)

ovs_vault_k8s_auth_backend_role = vault.kubernetes.AuthBackendRole(
    f"ovs-vault-k8s-auth-backend-role-{stack_info.env_suffix}",
    role_name=Services.odl_video_service,
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    bound_service_account_names=["*"],
    bound_service_account_namespaces=[ovs_namespace],
    token_policies=[ovs_k8s_vault_policy.name],
)

vault_k8s_resources = OLVaultK8SResources(
    resource_config=OLVaultK8SResourcesConfig(
        application_name=Services.odl_video_service,
        namespace=ovs_namespace,
        labels=k8s_app_labels,
        vault_address=vault_config.require("address"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        vault_auth_role_name=ovs_vault_k8s_auth_backend_role.role_name,
    ),
    opts=ResourceOptions(delete_before_replace=True),
)

# RDS endpoint for K8s secret templates
rds_endpoint = (
    f"{db_instance_name}.cbnm7ajau6mi.us-east-1.rds.amazonaws.com"
    f":{DEFAULT_POSTGRES_PORT}"
)

# Create K8s secrets
secret_names, secret_resources = create_ovs_k8s_secrets(
    stack_info=stack_info,
    ovs_namespace=ovs_namespace,
    k8s_global_labels=k8s_app_labels,
    vault_k8s_resources=vault_k8s_resources,
    db_config=ovs_db_vault_backend,
    rds_endpoint=rds_endpoint,
    redis_auth_token=redis_auth_token,
    redis_cluster=ovs_server_redis_cluster,
    use_shibboleth=use_shibboleth,
)

# Merge stack-level config vars into the app env vars
k8s_extra_vars: dict[str, str | Output[str]] = {
    **(ovs_config.get_object("k8s_vars") or {}),
    # MediaConvert-related vars that need resource outputs
    "AWS_ROLE_NAME": ovs_mediaconvert.role.name,
    "AWS_ACCOUNT_ID": aws_account.account_id,
    "POST_TRANSCODE_ACTIONS": "cloudsync.api.process_transcode_results",
    "TRANSCODE_JOB_TEMPLATE": "./config/mediaconvert.json",
    "EDX_BASE_URL": ovs_config.get("edx_base_url") or "",
    "VIDEO_TRANSCODE_QUEUE": ovs_mediaconvert.queue.name,
}
app_env_vars.update(k8s_extra_vars)

if "ODL_VIDEO_SERVICE_DOCKER_TAG" not in os.environ:
    msg = "ODL_VIDEO_SERVICE_DOCKER_TAG must be set."
    raise OSError(msg)
ODL_VIDEO_SERVICE_DOCKER_TAG = os.environ["ODL_VIDEO_SERVICE_DOCKER_TAG"]

# NGINX configuration for K8s (HTTP only — APISIX handles TLS)
ovs_domains = ovs_config.get_object("domains") or [ovs_config.get("default_domain")]
server_names = " ".join(ovs_domains)
default_domain = ovs_config.get("default_domain")

nginx_with_shib_conf = f"""\
server {{
    listen 443 ssl default_server;
    listen [::]:443;
    server_name {default_domain};

    ssl_certificate /etc/nginx/cert.pem;
    ssl_certificate_key /etc/nginx/key.pem;
    ssl_stapling on;
    ssl_stapling_verify on;
    ssl_session_timeout 1d;
    ssl_session_tickets off;
    # ssl_protocols TLSv1 TLSv1.1 TLSv1.2 TLSv1.3;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256:TLS_AES_128_GCM_SHA256:DHE-RSA-AES256-GCM-SHA384:DHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-SHA256:DHE-RSA-AES128-SHA256:ECDHE-RSA-AES256-SHA384:ECDHE-RSA-AES128-SHA256;
    ssl_prefer_server_ciphers on;
    resolver 1.1.1.1;

    # 1. Determine the 'real' scheme from the Ingress
    set $my_scheme $http_x_forwarded_proto;
    if ($my_scheme = "") {{ set $my_scheme $scheme; }}

    # 2. Map HTTPS variable for Shibboleth/uWSGI
    set $my_https "";
    if ($my_scheme = "https") {{ set $my_https "on"; }}

    root /opt/odl-video-service/;

    location /shibauthorizer {{
        internal;
        include fastcgi_params;
        include shib_fastcgi_params;
        # Tell Shibboleth the request IS secure
        fastcgi_param SERVER_PORT 443;
        fastcgi_param REQUEST_SCHEME https;
        fastcgi_param HTTPS on;
        fastcgi_pass unix:/opt/shibboleth/shibauthorizer.sock;
    }}

    location /Shibboleth.sso {{
        include fastcgi_params;
        include shib_fastcgi_params;
        # Tell the responder the request IS secure
        fastcgi_param SERVER_PORT 443;
        fastcgi_param REQUEST_SCHEME https;
        fastcgi_param HTTPS on;
        fastcgi_pass unix:/opt/shibboleth/shibresponder.sock;
    }}

    location /login {{
        include shib_clear_headers;
        proxy_set_header Host {default_domain};
        shib_request /shibauthorizer;
        shib_request_use_headers on;
        include shib_params;
        include uwsgi_params;

        # Override uwsgi_params to reflect the Ingress scheme
        uwsgi_param UWSGI_SCHEME $my_scheme;
        uwsgi_param HTTPS $my_https;

        uwsgi_ignore_client_abort on;
        uwsgi_pass 127.0.0.1:8087;
    }}

    location / {{
        include uwsgi_params;
        uwsgi_param UWSGI_SCHEME $my_scheme;
        uwsgi_param HTTPS $my_https;
        uwsgi_pass 127.0.0.1:8087;
    }}

    location = /nginx-health {{
        access_log off;
        return 200 "healthy\n";
    }}

    location /collections/letterlocking {{
        return 301 https://www.youtube.com/c/Letterlocking/videos;
    }}

    location /collections/letterlocking/videos {{
        return 301 https://www.youtube.com/c/Letterlocking/videos;
    }}

    location /collections/letterlocking/videos/30213-iron-gall-ink-a-quick-and-easy-method {{
        return 301 https://www.youtube.com/playlist?list=PL2uZTM-xaHP4tFQT7eTTK3sWRoJMcDWwB;
    }}

    location /collections/letterlocking/videos/30215-elizabeth-stuart-s-deciphering-sir-thomas-roe-s-letter-cryptography-1626 {{
        return 301 https://www.youtube.com/watch?v=6X_ZXrLs8I8&list=PL2uZTM-xaHP4tFQT7eTTK3sWRoJMcDWwB&index=3&t=0s;
    }}

    location /collections/letterlocking/videos/30209-a-tiny-spy-letter-constantijn-huygens-to-amalia-von-solms-1635 {{
        return 301 https://www.youtube.com/watch?v=PePWd-h679c&list=PL2uZTM-xaHP4tFQT7eTTK3sWRoJMcDWwB&index=7&t=0s;
    }}

    location /collections/c8c5179c7596408fa0f09f6b76082331 {{
        return 301 https://www.youtube.com/c/MITEnergyInitiative;
    }}
}}
"""  # noqa: E501

nginx_wo_shib_conf = f"""\
server {{
    listen 443 ssl default_server;
    listen [::]:443;
    server_name {default_domain};

    ssl_certificate /etc/nginx/cert.pem;
    ssl_certificate_key /etc/nginx/key.pem;
    ssl_stapling on;
    ssl_stapling_verify on;
    ssl_session_timeout 1d;
    ssl_session_tickets off;
    # ssl_protocols TLSv1 TLSv1.1 TLSv1.2 TLSv1.3;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256:TLS_AES_128_GCM_SHA256:DHE-RSA-AES256-GCM-SHA384:DHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-SHA256:DHE-RSA-AES128-SHA256:ECDHE-RSA-AES256-SHA384:ECDHE-RSA-AES128-SHA256;
    ssl_prefer_server_ciphers on;
    resolver 1.1.1.1;

    location = /nginx-health {{
        access_log off;
        return 200 "healthy\n";
        add_header Content-Type text/plain;
    }}

    location / {{
        include uwsgi_params;
        uwsgi_ignore_client_abort on;
        uwsgi_pass 127.0.0.1:8087;
    }}

    location /collections/letterlocking {{
        return 301 https://www.youtube.com/c/Letterlocking/videos;
    }}

    location /collections/letterlocking/videos {{
        return 301 https://www.youtube.com/c/Letterlocking/videos;
    }}

    location /collections/letterlocking/videos/30213-iron-gall-ink-a-quick-and-easy-method {{
        return 301 https://www.youtube.com/playlist?list=PL2uZTM-xaHP4tFQT7eTTK3sWRoJMcDWwB;
    }}

    location /collections/letterlocking/videos/30215-elizabeth-stuart-s-deciphering-sir-thomas-roe-s-letter-cryptography-1626 {{
        return 301 https://www.youtube.com/watch?v=6X_ZXrLs8I8&list=PL2uZTM-xaHP4tFQT7eTTK3sWRoJMcDWwB&index=3&t=0s;
    }}

    location /collections/letterlocking/videos/30209-a-tiny-spy-letter-constantijn-huygens-to-amalia-von-solms-1635 {{
        return 301 https://www.youtube.com/watch?v=PePWd-h679c&list=PL2uZTM-xaHP4tFQT7eTTK3sWRoJMcDWwB&index=7&t=0s;
    }}

    location /collections/c8c5179c7596408fa0f09f6b76082331 {{
        return 301 https://www.youtube.com/c/MITEnergyInitiative;
    }}
}}
"""  # noqa: E501

nginx_conf_content = nginx_with_shib_conf if use_shibboleth else nginx_wo_shib_conf

shibboleth2_xml = f"""\
<SPConfig xmlns="urn:mace:shibboleth:3.0:native:sp:config"
          xmlns:conf="urn:mace:shibboleth:3.0:native:sp:config"
          clockSkew="180">

    <OutOfProcess tranLogFormat="%u|%s|%IDP|%i|%ac|%t|%attr|%n|%b|%E|%S|%SS|%L|%UA|%a" />

    <RequestMapper type="Native">
        <RequestMap>
            <Host authType="shibboleth" name="{default_domain}" requireSession="true" scheme="https" port="443"/>
        </RequestMap>
    </RequestMapper>

    <ApplicationDefaults entityID="https://{default_domain}/shibboleth"
                         REMOTE_USER="eppn subject-id pairwise-id persistent-id"
                         cipherSuites="DEFAULT:!EXP:!LOW:!aNULL:!eNULL:!DES:!IDEA:!SEED:!RC4:!3DES:!kRSA:!SSLv2:!SSLv3:!TLSv1:!TLSv1.1">

        <Sessions lifetime="28800" timeout="3600" relayState="ss:mem"
                  checkAddress="false" handlerSSL="false" cookieProps="https"
                  redirectLimit="exact+whitelist"
                  redirectWhitelist="https://idp.mit.edu/ https://idp.touchstonenetwork.net/ https://idp-alum.mit.edu/">

            <SSO discoveryProtocol="SAMLDS" discoveryURL="https://wayf.mit.edu/DS">
                SAML2
            </SSO>

            <Logout>SAML2 Local</Logout>
            <LogoutInitiator type="Admin" Location="/Logout/Admin" acl="127.0.0.1 ::1" />
            <Handler type="MetadataGenerator" Location="/Metadata" signing="false"/>
            <Handler type="Status" Location="/Status" acl="127.0.0.1 ::1"/>
            <Handler type="Session" Location="/Session" showAttributeValues="true"/>
            <Handler type="DiscoveryFeed" Location="/DiscoFeed"/>
        </Sessions>

        <Errors supportContact="odl-devops@mit.edu"
                helpLocation="/about.html"
                styleSheet="/shibboleth-sp/main.css"/>

        <MetadataProvider type="XML" validate="true" url="http://touchstone.mit.edu/metadata/MIT-metadata.xml" backingFilePath="MIT-metadata.xml" maxRefreshDelay="7200">
            <MetadataFilter type="RequireValidUntil" maxValidityInterval="5184000"/>
            <MetadataFilter type="Signature" certificate="mit-md-cert.pem" verifyBackup="false"/>
        </MetadataProvider>

        <TrustEngine type="ExplicitKey" />
        <AttributeExtractor type="XML" validate="true" reloadChanges="false" path="attribute-map.xml"/>
        <AttributeFilter type="XML" validate="true" path="attribute-policy.xml"/>
        <AttributeResolver type="Query" subjectMatch="true"/>

        <CredentialResolver type="File" use="signing" key="sp-key.pem" certificate="sp-cert.pem"/>
        <CredentialResolver type="File" use="encryption" key="sp-key.pem" certificate="sp-cert.pem"/>

    </ApplicationDefaults>

    <SecurityPolicyProvider type="XML" validate="true" path="security-policy.xml"/>
    <ProtocolProvider type="XML" validate="true" reloadChanges="false" path="protocols.xml"/>

</SPConfig>
"""  # noqa: E501

# Static NGINX support files
bilder_files_dir = Path(__file__).resolve().parent / "files"

fastcgi_params_content = bilder_files_dir.joinpath("fastcgi_params").read_text()
uwsgi_params_content = bilder_files_dir.joinpath("uwsgi_params").read_text()
logging_conf_content = bilder_files_dir.joinpath("logging.conf").read_text()

# Shibboleth-specific ConfigMap, volumes, and mounts (consolidated)
if use_shibboleth:
    attribute_map_content = bilder_files_dir.joinpath("attribute-map.xml").read_text()
    shib_extra_nginx_data: dict[str, str] = {
        "shib_clear_headers": bilder_files_dir.joinpath(
            "shib_clear_headers"
        ).read_text(),
        "shib_fastcgi_params": bilder_files_dir.joinpath(
            "shib_fastcgi_params"
        ).read_text(),
        "shib_params": bilder_files_dir.joinpath("shib_params").read_text(),
    }
    shib_configmap: kubernetes.core.v1.ConfigMap | None = kubernetes.core.v1.ConfigMap(
        f"ovs-shib-configmap-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="ovs-shib-config",
            namespace=ovs_namespace,
            labels=k8s_app_labels,
        ),
        data={
            "shibboleth2.xml": shibboleth2_xml,
            "attribute-map.xml": attribute_map_content,
        },
    )
    shib_extra_volumes: list[kubernetes.core.v1.VolumeArgs] = [
        kubernetes.core.v1.VolumeArgs(
            name="shib-config",
            config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                name="ovs-shib-config",
            ),
        ),
        kubernetes.core.v1.VolumeArgs(
            name="shib-certs",
            secret=kubernetes.core.v1.SecretVolumeSourceArgs(
                secret_name="ovs-shib-certs-static-secret",  # noqa: S106  # pragma: allowlist secret
            ),
        ),
    ]
    shib_extra_nginx_mounts: list[kubernetes.core.v1.VolumeMountArgs] = [
        kubernetes.core.v1.VolumeMountArgs(
            name="nginx-config",
            mount_path="/etc/nginx/shib_clear_headers",
            sub_path="shib_clear_headers",
            read_only=True,
        ),
        kubernetes.core.v1.VolumeMountArgs(
            name="nginx-config",
            mount_path="/etc/nginx/shib_fastcgi_params",
            sub_path="shib_fastcgi_params",
            read_only=True,
        ),
        kubernetes.core.v1.VolumeMountArgs(
            name="nginx-config",
            mount_path="/etc/nginx/shib_params",
            sub_path="shib_params",
            read_only=True,
        ),
        kubernetes.core.v1.VolumeMountArgs(
            name="shib-config",
            mount_path="/etc/shibboleth/shibboleth2.xml",
            sub_path="shibboleth2.xml",
            read_only=True,
        ),
        kubernetes.core.v1.VolumeMountArgs(
            name="shib-config",
            mount_path="/etc/shibboleth/attribute-map.xml",
            sub_path="attribute-map.xml",
            read_only=True,
        ),
        kubernetes.core.v1.VolumeMountArgs(
            name="shib-certs",
            mount_path="/etc/shibboleth/sp-cert.pem",
            sub_path="sp-cert.pem",
            read_only=True,
        ),
        kubernetes.core.v1.VolumeMountArgs(
            name="shib-certs",
            mount_path="/etc/shibboleth/sp-key.pem",
            sub_path="sp-key.pem",
            read_only=True,
        ),
        kubernetes.core.v1.VolumeMountArgs(
            name="shib-certs",
            mount_path="/etc/shibboleth/mit-md-cert.pem",
            sub_path="mit-md-cert.pem",
            read_only=True,
        ),
    ]
else:
    shib_configmap = None
    shib_extra_nginx_data = {}
    shib_extra_volumes = []
    shib_extra_nginx_mounts = []

# ConfigMap for NGINX configuration files
nginx_configmap_data: dict[str, str] = {
    "default.conf": nginx_conf_content,
    "fastcgi_params": fastcgi_params_content,
    "uwsgi_params": uwsgi_params_content,
    "logging.conf": logging_conf_content,
    **shib_extra_nginx_data,
}

nginx_configmap = kubernetes.core.v1.ConfigMap(
    f"ovs-nginx-configmap-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="ovs-nginx-config",
        namespace=ovs_namespace,
        labels=k8s_app_labels,
    ),
    data=nginx_configmap_data,
)

# ConfigMap for non-sensitive application environment variables
app_configmap = kubernetes.core.v1.ConfigMap(
    f"ovs-app-configmap-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="ovs-app-config",
        namespace=ovs_namespace,
        labels=k8s_app_labels,
    ),
    data={k: v if isinstance(v, Output) else str(v) for k, v in app_env_vars.items()},
)

# Build env_from references for the deployment
env_from_sources = [
    kubernetes.core.v1.EnvFromSourceArgs(
        config_map_ref=kubernetes.core.v1.ConfigMapEnvSourceArgs(
            name="ovs-app-config",
        ),
    ),
] + [
    kubernetes.core.v1.EnvFromSourceArgs(
        secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
            name=secret_name,
        ),
    )
    for secret_name in secret_names
]

# Volume definitions
volumes: list[kubernetes.core.v1.VolumeArgs] = [
    kubernetes.core.v1.VolumeArgs(
        name="nginx-config",
        config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
            name="ovs-nginx-config",
        ),
    ),
    kubernetes.core.v1.VolumeArgs(
        name="staticfiles",
        empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
    ),
    *shib_extra_volumes,
]

# NGINX sidecar volume mounts
nginx_volume_mounts: list[kubernetes.core.v1.VolumeMountArgs] = [
    kubernetes.core.v1.VolumeMountArgs(
        name="nginx-config",
        mount_path="/etc/nginx/conf.d/default.conf",
        sub_path="default.conf",
        read_only=True,
    ),
    kubernetes.core.v1.VolumeMountArgs(
        name="nginx-config",
        mount_path="/etc/nginx/fastcgi_params",
        sub_path="fastcgi_params",
        read_only=True,
    ),
    kubernetes.core.v1.VolumeMountArgs(
        name="nginx-config",
        mount_path="/etc/nginx/uwsgi_params",
        sub_path="uwsgi_params",
        read_only=True,
    ),
    kubernetes.core.v1.VolumeMountArgs(
        name="nginx-config",
        mount_path="/etc/nginx/logging.conf",
        sub_path="logging.conf",
        read_only=True,
    ),
    kubernetes.core.v1.VolumeMountArgs(
        name="staticfiles",
        mount_path="/opt/odl-video-service/staticfiles",
        read_only=True,
    ),
    *shib_extra_nginx_mounts,
]

# Init container for migrations and collectstatic
init_container = kubernetes.core.v1.ContainerArgs(
    name="ovs-init",
    image=f"mitodl/odl-video-service-app:{ODL_VIDEO_SERVICE_DOCKER_TAG}",
    command=["/bin/bash", "-c"],
    args=[
        "python3 manage.py migrate --noinput && "
        "python3 manage.py collectstatic --noinput"
    ],
    env_from=env_from_sources,
    volume_mounts=[
        kubernetes.core.v1.VolumeMountArgs(
            name="staticfiles",
            mount_path="/src/staticfiles",
        ),
    ],
    resources=kubernetes.core.v1.ResourceRequirementsArgs(
        requests={"cpu": "100m", "memory": "256Mi"},
        limits={"memory": "512Mi"},
    ),
)

# Main app container (uWSGI)
app_container = kubernetes.core.v1.ContainerArgs(
    name="ovs-app",
    image=f"mitodl/odl-video-service-app:{ODL_VIDEO_SERVICE_DOCKER_TAG}",
    command=["uwsgi"],
    args=["uwsgi.ini"],
    ports=[
        kubernetes.core.v1.ContainerPortArgs(
            container_port=8087,
            name="uwsgi",
            protocol="TCP",
        ),
    ],
    env_from=env_from_sources,
    volume_mounts=[
        kubernetes.core.v1.VolumeMountArgs(
            name="staticfiles",
            mount_path="/src/staticfiles",
        ),
    ],
    resources=kubernetes.core.v1.ResourceRequirementsArgs(
        requests={"cpu": "250m", "memory": "512Mi"},
        limits={"memory": "1Gi"},
    ),
)

# NGINX + Shibboleth sidecar container
nginx_sidecar = kubernetes.core.v1.ContainerArgs(
    name="nginx-shib",
    image="pennlabs/shibboleth-sp-nginx:latest",
    ports=[
        kubernetes.core.v1.ContainerPortArgs(
            container_port=443,
            name="https",
            protocol="TCP",
        ),
    ],
    volume_mounts=nginx_volume_mounts,
    resources=kubernetes.core.v1.ResourceRequirementsArgs(
        requests={"cpu": "50m", "memory": "128Mi"},
        limits={"memory": "256Mi"},
    ),
    liveness_probe=kubernetes.core.v1.ProbeArgs(
        http_get=kubernetes.core.v1.HTTPGetActionArgs(
            path="/nginx-health",
            port=443,
            scheme="HTTPS",
        ),
        initial_delay_seconds=30,
        period_seconds=30,
        failure_threshold=3,
        timeout_seconds=5,
    ),
    readiness_probe=kubernetes.core.v1.ProbeArgs(
        http_get=kubernetes.core.v1.HTTPGetActionArgs(
            path="/nginx-health",
            port=443,
            scheme="HTTPS",
        ),
        initial_delay_seconds=15,
        period_seconds=15,
        failure_threshold=3,
        timeout_seconds=5,
    ),
    startup_probe=kubernetes.core.v1.ProbeArgs(
        http_get=kubernetes.core.v1.HTTPGetActionArgs(
            path="/nginx-health",
            port=443,
            scheme="HTTPS",
        ),
        initial_delay_seconds=10,
        period_seconds=10,
        failure_threshold=6,
        success_threshold=1,
        timeout_seconds=5,
    ),
)

# Deployment
ovs_deployment = kubernetes.apps.v1.Deployment(
    f"ovs-deployment-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="ovs",
        namespace=ovs_namespace,
        labels={
            **k8s_app_labels,
            "ol.mit.edu/component": "webapp",
        },
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        replicas=ovs_config.get_int("k8s_replicas") or 2,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels={
                "ol.mit.edu/application": Application.odl_video_service,
                "ol.mit.edu/component": "webapp",
            },
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels={
                    **k8s_app_labels,
                    "ol.mit.edu/component": "webapp",
                    "ol.mit.edu/pod-security-group": ovs_app_security_group.id,
                },
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                init_containers=[init_container],
                containers=[app_container, nginx_sidecar],
                volumes=volumes,
                affinity=kubernetes.core.v1.AffinityArgs(
                    pod_anti_affinity=kubernetes.core.v1.PodAntiAffinityArgs(
                        preferred_during_scheduling_ignored_during_execution=[
                            kubernetes.core.v1.WeightedPodAffinityTermArgs(
                                weight=100,
                                pod_affinity_term=kubernetes.core.v1.PodAffinityTermArgs(
                                    label_selector=kubernetes.meta.v1.LabelSelectorArgs(
                                        match_labels={
                                            "ol.mit.edu/application": Application.odl_video_service,  # noqa: E501
                                        },
                                    ),
                                    topology_key="kubernetes.io/hostname",
                                ),
                            ),
                        ],
                    ),
                ),
            ),
        ),
    ),
    opts=ResourceOptions(
        depends_on=[
            ovs_app_security_group,
            app_configmap,
            nginx_configmap,
            *secret_resources,
            *([] if shib_configmap is None else [shib_configmap]),
        ]
    ),
)

# Service for the deployment (points to NGINX sidecar port)
ovs_service_name = "ovs-webapp"
ovs_service = kubernetes.core.v1.Service(
    f"ovs-service-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=ovs_service_name,
        namespace=ovs_namespace,
        labels=k8s_app_labels,
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        type="ClusterIP",
        selector={
            "ol.mit.edu/application": Application.odl_video_service,
            "ol.mit.edu/component": "webapp",
        },
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                name="http",
                port=DEFAULT_HTTPS_PORT,
                target_port="https",
                protocol="TCP",
                app_protocol="https",
            ),
        ],
    ),
)

# Celery worker deployment (no NGINX sidecar needed)
celery_log_level = ovs_config.get("log_level") or "INFO"
celery_deployment = kubernetes.apps.v1.Deployment(
    f"ovs-celery-deployment-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="ovs-celery",
        namespace=ovs_namespace,
        labels={
            **k8s_app_labels,
            "ol.mit.edu/component": "celery",
        },
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        replicas=1,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels={
                "ol.mit.edu/application": Application.odl_video_service,
                "ol.mit.edu/component": "celery",
            },
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels={
                    **k8s_app_labels,
                    "ol.mit.edu/component": "celery",
                    "ol.mit.edu/pod-security-group": ovs_app_security_group.id,
                },
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="ovs-celery",
                        image=f"mitodl/odl-video-service-app:{ODL_VIDEO_SERVICE_DOCKER_TAG}",
                        command=["celery"],
                        args=[
                            "-A",
                            "odl_video",
                            "worker",
                            "-B",
                            "-l",
                            celery_log_level,
                        ],
                        env_from=env_from_sources,
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={"cpu": "100m", "memory": "3000Mi"},
                            limits={"memory": "3000Mi"},
                        ),
                    ),
                ],
            ),
        ),
    ),
    opts=ResourceOptions(
        depends_on=[
            ovs_app_security_group,
            app_configmap,
            *secret_resources,
        ]
    ),
)

tls_secret_name = "ovs-tls-pair"  # noqa: S105  # pragma: allowlist secret
cert_manager_certificate = OLCertManagerCert(
    f"ovs-cert-manager-certificate-{stack_info.env_suffix}",
    cert_config=OLCertManagerCertConfig(
        application_name="ovs",
        k8s_namespace=ovs_namespace,
        k8s_labels=k8s_app_labels,
        create_apisixtls_resource=True,
        dest_secret_name=tls_secret_name,
        dns_names=[default_domain],
    ),
)

ovs_apisix_httproute = OLApisixRoute(
    f"ovs-apisix-httproute-{stack_info.env_suffix}",
    route_configs=[
        OLApisixRouteConfig(
            route_name="passthrough",
            hosts=[default_domain],
            paths=["/*"],
            backend_service_name=ovs_service_name,
            backend_service_port=443,
            plugins=[],
        ),
    ],
    k8s_namespace=ovs_namespace,
    k8s_labels=k8s_app_labels,
)

export(
    "odl_video_service_k8s",
    {
        "namespace": ovs_namespace,
        "service_name": "ovs-webapp",
    },
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
