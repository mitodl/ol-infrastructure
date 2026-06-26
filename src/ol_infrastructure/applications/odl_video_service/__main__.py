"""The complete state needed to provision OVS running on Docker.

- Create S3 buckets for video storage
- Create a PostgreSQL database in AWS RDS
- Create a Redis cluster in AWS Elasticache
- Create an IAM policy to grant access to S3 and other resources
- Create MediaConvert resources for video transcoding
"""

import json
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import (
    ROOT_STACK_RESOURCE,
    Alias,
    Config,
    Output,
    ResourceOptions,
    export,
)
from pulumi_aws import ec2, get_caller_identity, iam
from pulumi_aws.s3 import BucketCorsConfigurationCorsRuleArgs

from bridge.lib.magic_numbers import (
    AWS_RDS_DEFAULT_DATABASE_CAPACITY,
    DEFAULT_NGINX_PORT,
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
from ol_infrastructure.components.services.apisix import (
    OLApisixRoute,
    OLApisixRouteConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.k8s import (
    GranianConfig,
    OLApplicationK8s,
    OLApplicationK8sCeleryBeatConfig,
    OLApplicationK8sCeleryWorkerConfig,
    OLApplicationK8sConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib import pulumi_projects as projects
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
from ol_infrastructure.lib.pulumi_helper import (
    docker_image_config_kwargs,
    make_stack_reference,
    merge_otel_resource_attributes,
    parse_stack,
)
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

# Configuration items and initialziations
if Config("vault_server").get("env_namespace"):
    setup_vault_provider()
ovs_config = Config("ovs")
stack_info = parse_stack()

aws_account = get_caller_identity()

network_stack = make_stack_reference(projects.NETWORKING, stack_info.name)
policy_stack = make_stack_reference(projects.POLICIES, "default")
dns_stack = make_stack_reference(projects.DNS, "default")
vault_stack = make_stack_reference(
    projects.VAULT_SERVER, f"operations.{stack_info.name}"
)

target_vpc_name = ovs_config.get("target_vpc") or "applications_vpc"
target_vpc = network_stack.require_output(target_vpc_name)
target_vpc_id = target_vpc["id"]
data_vpc = network_stack.require_output("data_vpc")
operations_vpc = network_stack.require_output("operations_vpc")

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

env_name = f"odl_video_service-{stack_info.env_suffix}"

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
    intelligent_tiering_archive_access_days=None,  # CloudFront origin
    intelligent_tiering_deep_archive_access_days=None,
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
    intelligent_tiering_archive_access_days=None,  # CloudFront origin
    intelligent_tiering_deep_archive_access_days=None,
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
    intelligent_tiering_archive_access_days=None,  # CloudFront origin
    intelligent_tiering_deep_archive_access_days=None,
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
    intelligent_tiering_archive_access_days=None,  # CloudFront origin
    intelligent_tiering_deep_archive_access_days=None,
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
                "s3:AbortMultipartUpload",
                "s3:PutBucketWebsite",
                "s3:PutObject",
                "s3:PutObjectTagging",
                "s3:ReplicateDelete",
                "s3:ReplicateObject",
                "s3:RestoreObject",
            ],
            "Effect": "Allow",
            "Resource": [
                f"arn:aws:s3:::{ovs_config.get('s3_bucket_name')}",
                f"arn:aws:s3:::{ovs_config.get('s3_subtitle_bucket_name')}",
                f"arn:aws:s3:::{ovs_config.get('s3_thumbnail_bucket_name')}",
                f"arn:aws:s3:::{ovs_config.get('s3_transcode_bucket_name')}",
                f"arn:aws:s3:::{ovs_config.get('s3_watch_bucket_name')}",
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
    path=f"/ol-applications/odl-video-service-server/odl_video_service/{stack_info.env_suffix}/",
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
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=operations_vpc["k8s_pod_subnet_cidrs"],
            protocol="tcp",
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            description="Allow Operations VPC celery monitoring pods to Redis",
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
    "REDIS_MAX_CONNECTIONS": redis_config.get("max_connections") or "65000",
    "STATUS_TOKEN": stack_info.env_suffix,
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

cluster_stack = make_stack_reference(projects.EKS, f"applications.{stack_info.name}")
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

# Unconditionally append k8s labels to OTEL_RESOURCE_ATTRIBUTES so all telemetry
# signals carry organizational metadata regardless of stack environment.
merge_otel_resource_attributes(app_env_vars, k8s_app_labels)

default_domain = ovs_config.get("default_domain")
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

celery_log_level = ovs_config.get("log_level") or "INFO"

ovs_k8s_app = OLApplicationK8s(
    ol_app_k8s_config=OLApplicationK8sConfig(
        project_root=Path(__file__).parent,
        application_config=app_env_vars,
        application_name=Services.odl_video_service,
        application_namespace=ovs_namespace,
        application_lb_service_name="ovs-webapp",
        application_lb_service_port_name="http",
        application_min_replicas=ovs_config.get_int("k8s_replicas") or 2,
        k8s_global_labels=k8s_app_labels,
        env_from_secret_names=secret_names,
        application_security_group_id=ovs_app_security_group.id,
        application_security_group_name=ovs_app_security_group.name,
        application_image_repository="mitodl/odl-video-service-app",
        **docker_image_config_kwargs("ODL_VIDEO_SERVICE"),
        granian_config=GranianConfig(
            application_module="odl_video.wsgi:application",
            workers=2,
            blocking_threads_idle_timeout=120,
            enable_metrics=True,
            log_level=(ovs_config.get("log_level") or "info").lower(),
        ),
        import_nginx_config=True,
        import_nginx_config_path="files/web.conf_granian",
        vault_k8s_resource_auth_name=vault_k8s_resources.auth_name,
        init_migrations=True,
        init_collectstatic=True,
        resource_requests={"cpu": "250m", "memory": "512Mi"},
        resource_limits={"memory": "1Gi"},
        probe_configs={
            "liveness_probe": kubernetes.core.v1.ProbeArgs(
                http_get=kubernetes.core.v1.HTTPGetActionArgs(
                    path="/nginx-health",
                    port=DEFAULT_NGINX_PORT,
                ),
                initial_delay_seconds=30,
                period_seconds=30,
                failure_threshold=3,
                timeout_seconds=5,
            ),
            "readiness_probe": kubernetes.core.v1.ProbeArgs(
                http_get=kubernetes.core.v1.HTTPGetActionArgs(
                    path="/nginx-health",
                    port=DEFAULT_NGINX_PORT,
                ),
                initial_delay_seconds=15,
                period_seconds=15,
                failure_threshold=3,
                timeout_seconds=5,
            ),
            "startup_probe": kubernetes.core.v1.ProbeArgs(
                http_get=kubernetes.core.v1.HTTPGetActionArgs(
                    path="/nginx-health",
                    port=DEFAULT_NGINX_PORT,
                ),
                initial_delay_seconds=10,
                period_seconds=10,
                failure_threshold=12,
                success_threshold=1,
                timeout_seconds=5,
            ),
        },
        celery_worker_configs=[
            OLApplicationK8sCeleryWorkerConfig(
                application_name="odl_video",
                worker_name="celery",
                log_level=celery_log_level,
                redis_host=ovs_server_redis_cluster.address,
                redis_password=redis_auth_token,
                resource_requests={"cpu": "100m", "memory": "3000Mi"},
                resource_limits={"memory": "3000Mi"},
                min_replicas=1,
                max_replicas=3,
            ),
        ],
        celery_beat_config=OLApplicationK8sCeleryBeatConfig(
            application_name="odl_video",
            log_level=celery_log_level,
            resource_requests={"cpu": "10m", "memory": "512Mi"},
            resource_limits={"memory": "512Mi"},
        ),
        webapp_deployment_aliases=[
            Alias(
                name=f"ovs-deployment-{stack_info.env_suffix}",
                parent=ROOT_STACK_RESOURCE,
            )
        ],
        webapp_service_aliases=[
            Alias(
                name=f"ovs-service-{stack_info.env_suffix}",
                parent=ROOT_STACK_RESOURCE,
            )
        ],
    ),
    opts=ResourceOptions(
        depends_on=[
            ovs_app_security_group,
            cert_manager_certificate,
            *secret_resources,
        ]
    ),
)

ovs_apisix_httproute = OLApisixRoute(
    f"ovs-apisix-httproute-{stack_info.env_suffix}",
    route_configs=[
        OLApisixRouteConfig(
            route_name="passthrough",
            hosts=[default_domain],
            paths=["/*"],
            backend_service_name="ovs-webapp",
            backend_service_port=DEFAULT_NGINX_PORT,
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

export(
    "odl_video_service",
    {
        "rds_host": db_address,
        "redis_token": ovs_server_redis_cluster.cache_cluster.auth_token,
        "redis": ovs_server_redis_cluster.address,
        "mediaconvert_queue": ovs_mediaconvert.queue.id,
    },
)

# CloudFront distribution and OAI resources (imported from pre-existing AWS resources)
from ol_infrastructure.applications.odl_video_service import (  # noqa: E402
    cloudfront,  # noqa: F401
)
