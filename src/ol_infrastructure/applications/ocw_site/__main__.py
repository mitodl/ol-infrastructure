import base64
import json
from pathlib import Path

import httpx
import pulumi_fastly as fastly
from pulumi import (
    ROOT_STACK_RESOURCE,
    Alias,
    Config,
    InvokeOptions,
    Output,
    ResourceOptions,
    export,
)
from pulumi_aws import get_caller_identity, iam, route53, s3

from bridge.lib.magic_numbers import (
    DEFAULT_HTTPS_PORT,
    FIVE_MINUTES,
    HTTP_STATUS_NOT_FOUND,
    HTTP_STATUS_OK,
    ONE_MEGABYTE_BYTE,
    SECONDS_IN_ONE_DAY,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.aws.route53_helper import (
    fastly_certificate_validation_records,
)
from ol_infrastructure.lib.fastly import (
    build_fastly_log_format_string,
    get_fastly_provider,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import (
    make_stack_reference,
    parse_stack,
)

ocw_site_config = Config("ocw_site")
stack_info = parse_stack()
aws_config = AWSBase(
    tags={
        "OU": "open-courseware",
        "Environment": f"applications_{stack_info.env_suffix}",
    }
)
fastly_provider = get_fastly_provider()

dns_stack = make_stack_reference(projects.DNS, "default")
ocw_zone = dns_stack.require_output("ocw")

vector_log_proxy_stack = make_stack_reference(
    projects.VECTOR_LOG_PROXY, f"operations.{stack_info.name}"
)
vector_log_proxy_domain = vector_log_proxy_stack.require_output(
    "vector_log_proxy_domain"
)

vector_log_proxy_secrets = read_yaml_secrets(
    Path(f"vector/vector_log_proxy.{stack_info.env_suffix}.yaml")
)
fastly_proxy_credentials = vector_log_proxy_secrets["fastly"]
encoded_fastly_proxy_credentials = base64.b64encode(
    f"{fastly_proxy_credentials['username']}:{fastly_proxy_credentials['password']}".encode()
).decode("utf8")

monitoring_stack = make_stack_reference(projects.MONITORING, "default")
fastly_access_logging_bucket = monitoring_stack.require_output(
    "fastly_access_logging_bucket"
)
fastly_access_logging_iam_role = monitoring_stack.require_output(
    "fastly_access_logging_iam_role"
)

# Get AWS account ID for audit logging configuration
aws_account = get_caller_identity()

# Create dedicated S3 audit logging bucket for OCW site buckets
audit_log_bucket_name = f"ocw-site-audit-logs-{stack_info.env_suffix}"
audit_log_bucket_config = S3BucketConfig(
    bucket_name=audit_log_bucket_name,
    versioning_enabled=True,
    server_side_encryption_enabled=True,
    sse_algorithm="AES256",
    lifecycle_rules=[
        s3.BucketLifecycleConfigurationRuleArgs(
            id="transition-old-audit-logs",
            status="Enabled",
            transitions=[
                s3.BucketLifecycleConfigurationRuleTransitionArgs(
                    days=90,
                    storage_class="GLACIER",
                ),
                s3.BucketLifecycleConfigurationRuleTransitionArgs(
                    days=365,
                    storage_class="DEEP_ARCHIVE",
                ),
            ],
            expiration=s3.BucketLifecycleConfigurationRuleExpirationArgs(
                days=2555,  # ~7 years retention
            ),
        )
    ],
    bucket_policy_document=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Sid": "S3ServerAccessLogsPolicy",
                    "Effect": "Allow",
                    "Principal": {"Service": "logging.s3.amazonaws.com"},
                    "Action": "s3:PutObject",
                    "Resource": f"arn:aws:s3:::{audit_log_bucket_name}/*",
                    "Condition": {
                        "StringEquals": {
                            "aws:SourceAccount": aws_account.account_id,
                        }
                    },
                }
            ],
        }
    ),
    intelligent_tiering_enabled=False,
    tags=aws_config.tags,
)

audit_log_bucket = OLBucket(
    "ocw-site-audit-logs-bucket",
    config=audit_log_bucket_config,
)

# Create S3 buckets
# There are two buckets for each environment (QA, Production):
# One for the that environment's draft site (where authors test content
# changes), and one for the environment's live site.
# See http://docs.odl.mit.edu/ocw-next/s3-buckets

draft_bucket_name = f"ocw-content-draft-{stack_info.env_suffix}"
draft_bucket_arn = f"arn:aws:s3:::{draft_bucket_name}"
live_bucket_name = f"ocw-content-live-{stack_info.env_suffix}"
live_bucket_arn = f"arn:aws:s3:::{live_bucket_name}"
test_bucket_name = f"ocw-content-test-{stack_info.env_suffix}"
test_bucket_arn = f"arn:aws:s3:::{test_bucket_name}"

draft_backup_bucket_name = f"ocw-content-backup-draft-{stack_info.env_suffix}"
draft_backup_bucket_arn = f"arn:aws:s3:::{draft_backup_bucket_name}"
live_backup_bucket_name = f"ocw-content-backup-live-{stack_info.env_suffix}"
live_backup_bucket_arn = f"arn:aws:s3:::{live_backup_bucket_name}"

draft_offline_bucket_name = f"ocw-content-offline-draft-{stack_info.env_suffix}"
draft_offline_bucket_arn = f"arn:aws:s3:::{draft_offline_bucket_name}"
live_offline_bucket_name = f"ocw-content-offline-live-{stack_info.env_suffix}"
live_offline_bucket_arn = f"arn:aws:s3:::{live_offline_bucket_name}"
test_offline_bucket_name = f"ocw-content-offline-test-{stack_info.env_suffix}"
test_offline_bucket_arn = f"arn:aws:s3:::{test_offline_bucket_name}"

# Draft bucket
draft_bucket_config = S3BucketConfig(
    bucket_name=draft_bucket_name,
    versioning_enabled=True,
    ownership_controls="BucketOwnerPreferred",
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
        )
    ],
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
    restrict_public_buckets=False,
    bucket_policy_document=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": [
                        f"{draft_bucket_arn}/*",
                    ],
                }
            ],
        }
    ),
    logging_target_bucket=audit_log_bucket_name,
    logging_target_prefix=f"ocw-site/{draft_bucket_name}/",
    logging_target_object_key_format=s3.BucketLoggingTargetObjectKeyFormatArgs(
        partitioned_prefix=s3.BucketLoggingTargetObjectKeyFormatPartitionedPrefixArgs(
            partition_date_source="EventTime"
        )
    ),
    logging_expected_bucket_owner=aws_account.account_id,
    intelligent_tiering_archive_access_days=None,  # Fastly backend
    intelligent_tiering_deep_archive_access_days=None,
    tags=aws_config.tags,
)

draft_bucket = OLBucket(
    "ocw-content-draft-bucket",
    config=draft_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(name=draft_bucket_name, parent=ROOT_STACK_RESOURCE),
            Alias(
                name="ol-draft-bucket-ownership-controls",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(name="ol-draft-bucket-versioning", parent=ROOT_STACK_RESOURCE),
            Alias(name="ol-draft-bucket-cors", parent=ROOT_STACK_RESOURCE),
            Alias(name="ol-draft-bucket-public-access", parent=ROOT_STACK_RESOURCE),
            Alias(name="ol-draft-bucket-policy", parent=ROOT_STACK_RESOURCE),
        ],
        depends_on=[audit_log_bucket],
    ),
)

# test bucket
test_bucket_config = S3BucketConfig(
    bucket_name=test_bucket_name,
    versioning_enabled=True,
    ownership_controls="BucketOwnerPreferred",
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
        )
    ],
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
    restrict_public_buckets=False,
    bucket_policy_document=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": [
                        f"{test_bucket_arn}/*",
                    ],
                }
            ],
        }
    ),
    logging_target_bucket=audit_log_bucket_name,
    logging_target_prefix=f"ocw-site/{test_bucket_name}/",
    logging_target_object_key_format=s3.BucketLoggingTargetObjectKeyFormatArgs(
        partitioned_prefix=s3.BucketLoggingTargetObjectKeyFormatPartitionedPrefixArgs(
            partition_date_source="EventTime"
        )
    ),
    logging_expected_bucket_owner=aws_account.account_id,
    intelligent_tiering_archive_access_days=None,  # Fastly backend
    intelligent_tiering_deep_archive_access_days=None,
    tags=aws_config.tags,
)

test_bucket = OLBucket(
    "ocw-content-test-bucket",
    config=test_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(name=test_bucket_name, parent=ROOT_STACK_RESOURCE),
            Alias(name="ol-test-bucket-cors", parent=ROOT_STACK_RESOURCE),
            Alias(name="ol-test-bucket-ownership-controls", parent=ROOT_STACK_RESOURCE),
            Alias(name="ol-test-bucket-versioning", parent=ROOT_STACK_RESOURCE),
            Alias(name="ol-test-bucket-public-access", parent=ROOT_STACK_RESOURCE),
            Alias(name="ol-test-bucket-policy", parent=ROOT_STACK_RESOURCE),
        ],
        depends_on=[audit_log_bucket],
    ),
)

# live bucket
live_bucket_config = S3BucketConfig(
    bucket_name=live_bucket_name,
    versioning_enabled=True,
    ownership_controls="BucketOwnerPreferred",
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
        )
    ],
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
    restrict_public_buckets=False,
    bucket_policy_document=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": [
                        f"{live_bucket_arn}/*",
                    ],
                }
            ],
        }
    ),
    logging_target_bucket=audit_log_bucket_name,
    logging_target_prefix=f"ocw-site/{live_bucket_name}/",
    logging_target_object_key_format=s3.BucketLoggingTargetObjectKeyFormatArgs(
        partitioned_prefix=s3.BucketLoggingTargetObjectKeyFormatPartitionedPrefixArgs(
            partition_date_source="EventTime"
        )
    ),
    logging_expected_bucket_owner=aws_account.account_id,
    # Disable archive access tiers: this bucket is a live CDN origin and objects
    # that haven't been accessed in 90 days may still be needed for cache misses.
    # Archive/Deep Archive retrieval latency (minutes/hours) would cause request
    # failures. The lifecycle rule still transitions objects to INTELLIGENT_TIERING
    # for Frequent/Infrequent/Archive-Instant-Access savings (all instant retrieval).
    intelligent_tiering_archive_access_days=None,
    intelligent_tiering_deep_archive_access_days=None,
    tags=aws_config.tags,
)

live_bucket = OLBucket(
    "ocw-content-live-bucket",
    config=live_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(name=live_bucket_name, parent=ROOT_STACK_RESOURCE),
            Alias(name="ol-live-bucket-cors", parent=ROOT_STACK_RESOURCE),
            Alias(name="ol-live-bucket-ownership-controls", parent=ROOT_STACK_RESOURCE),
            Alias(name="ol-live-bucket-versioning", parent=ROOT_STACK_RESOURCE),
            Alias(name="ol-live-bucket-public-access", parent=ROOT_STACK_RESOURCE),
            Alias(name="ol-live-bucket-policy", parent=ROOT_STACK_RESOURCE),
        ],
        depends_on=[audit_log_bucket],
    ),
)

# draft_backup bucket
draft_backup_bucket_config = S3BucketConfig(
    bucket_name=draft_backup_bucket_name,
    versioning_enabled=True,
    ownership_controls="BucketOwnerPreferred",
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
        )
    ],
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
    restrict_public_buckets=False,
    bucket_policy_document=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": [
                        f"{draft_backup_bucket_arn}/*",
                    ],
                }
            ],
        }
    ),
    # Backup bucket: never CDN-served, safe for archive tiers.
    intelligent_tiering_archive_access_days=90,
    intelligent_tiering_deep_archive_access_days=180,
    tags=aws_config.tags,
)

draft_backup_bucket = OLBucket(
    "ocw-content-backup-draft-bucket",
    config=draft_backup_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(name=draft_backup_bucket_name, parent=ROOT_STACK_RESOURCE),
            Alias(name="ol-draft-backup-bucket-cors", parent=ROOT_STACK_RESOURCE),
            Alias(
                name="ol-draft-backup-bucket-ownership-controls",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(name="ol-draft-backup-bucket-versioning", parent=ROOT_STACK_RESOURCE),
            Alias(
                name="ol-draft-backup-bucket-public-access", parent=ROOT_STACK_RESOURCE
            ),
            Alias(name="ol-draft-backup-bucket-policy", parent=ROOT_STACK_RESOURCE),
        ],
        depends_on=[audit_log_bucket],
    ),
)

# live_backup bucket
live_backup_bucket_config = S3BucketConfig(
    bucket_name=live_backup_bucket_name,
    versioning_enabled=True,
    ownership_controls="BucketOwnerPreferred",
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
        )
    ],
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
    restrict_public_buckets=False,
    bucket_policy_document=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": [
                        f"{live_backup_bucket_arn}/*",
                    ],
                }
            ],
        }
    ),
    logging_target_bucket=audit_log_bucket_name,
    logging_target_prefix=f"ocw-site/{live_backup_bucket_name}/",
    logging_target_object_key_format=s3.BucketLoggingTargetObjectKeyFormatArgs(
        partitioned_prefix=s3.BucketLoggingTargetObjectKeyFormatPartitionedPrefixArgs(
            partition_date_source="EventTime"
        )
    ),
    logging_expected_bucket_owner=aws_account.account_id,
    # Backup bucket: never CDN-served, safe for archive tiers.
    intelligent_tiering_archive_access_days=90,
    intelligent_tiering_deep_archive_access_days=180,
    tags=aws_config.tags,
)

live_backup_bucket = OLBucket(
    "ocw-content-backup-live-bucket",
    config=live_backup_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(name=live_backup_bucket_name, parent=ROOT_STACK_RESOURCE),
            Alias(name="ol-live-backup-bucket-cors", parent=ROOT_STACK_RESOURCE),
            Alias(
                name="ol-live-backup-bucket-ownership-controls",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(name="ol-live-backup-bucket-versioning", parent=ROOT_STACK_RESOURCE),
            Alias(
                name="ol-live-backup-bucket-public-access", parent=ROOT_STACK_RESOURCE
            ),
            Alias(name="ol-live-backup-bucket-policy", parent=ROOT_STACK_RESOURCE),
        ],
        depends_on=[audit_log_bucket],
    ),
)

# draft_offline bucket
draft_offline_bucket_config = S3BucketConfig(
    bucket_name=draft_offline_bucket_name,
    versioning_enabled=True,
    ownership_controls="BucketOwnerPreferred",
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
        )
    ],
    website_configuration=s3.BucketWebsiteConfigurationArgs(
        bucket=draft_offline_bucket_name,
        index_document=s3.BucketWebsiteConfigurationIndexDocumentArgs(
            suffix="index.html",
        ),
        error_document=s3.BucketWebsiteConfigurationErrorDocumentArgs(
            key="error.html",
        ),
    ),
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
    restrict_public_buckets=False,
    bucket_policy_document=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": [
                        f"{draft_offline_bucket_arn}/*",
                    ],
                }
            ],
        }
    ),
    logging_target_bucket=audit_log_bucket_name,
    logging_target_prefix=f"ocw-site/{draft_offline_bucket_name}/",
    logging_target_object_key_format=s3.BucketLoggingTargetObjectKeyFormatArgs(
        partitioned_prefix=s3.BucketLoggingTargetObjectKeyFormatPartitionedPrefixArgs(
            partition_date_source="EventTime"
        )
    ),
    logging_expected_bucket_owner=aws_account.account_id,
    intelligent_tiering_archive_access_days=None,  # S3 website, user-facing downloads
    intelligent_tiering_deep_archive_access_days=None,
    tags=aws_config.tags,
)

draft_offline_bucket = OLBucket(
    "ocw-content-offline-draft-bucket",
    config=draft_offline_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(name=draft_offline_bucket_name, parent=ROOT_STACK_RESOURCE),
            Alias(name="ol-draft-offline-bucket-cors", parent=ROOT_STACK_RESOURCE),
            Alias(name="draft-offline-website", parent=ROOT_STACK_RESOURCE),
            Alias(
                name="ol-offline-backup-bucket-ownership-controls",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="ol-offline-backup-bucket-versioning", parent=ROOT_STACK_RESOURCE
            ),
            Alias(
                name="ol-offline-backup-bucket-public-access",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(name="ol-offline-backup-bucket-policy", parent=ROOT_STACK_RESOURCE),
        ],
        depends_on=[audit_log_bucket],
    ),
)

# live_offline bucket
live_offline_bucket_config = S3BucketConfig(
    bucket_name=live_offline_bucket_name,
    versioning_enabled=True,
    ownership_controls="BucketOwnerPreferred",
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
        )
    ],
    website_configuration=s3.BucketWebsiteConfigurationArgs(
        bucket=live_offline_bucket_name,
        index_document=s3.BucketWebsiteConfigurationIndexDocumentArgs(
            suffix="index.html",
        ),
        error_document=s3.BucketWebsiteConfigurationErrorDocumentArgs(
            key="error.html",
        ),
    ),
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
    restrict_public_buckets=False,
    bucket_policy_document=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": [
                        f"{live_offline_bucket_arn}/*",
                    ],
                }
            ],
        }
    ),
    logging_target_bucket=audit_log_bucket_name,
    logging_target_prefix=f"ocw-site/{live_offline_bucket_name}/",
    logging_target_object_key_format=s3.BucketLoggingTargetObjectKeyFormatArgs(
        partitioned_prefix=s3.BucketLoggingTargetObjectKeyFormatPartitionedPrefixArgs(
            partition_date_source="EventTime"
        )
    ),
    logging_expected_bucket_owner=aws_account.account_id,
    intelligent_tiering_archive_access_days=None,  # S3 website, user-facing downloads
    intelligent_tiering_deep_archive_access_days=None,
    tags=aws_config.tags,
)

live_offline_bucket = OLBucket(
    "ocw-content-offline-live-bucket",
    config=live_offline_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(name=live_offline_bucket_name, parent=ROOT_STACK_RESOURCE),
            Alias(name="live-offline-website", parent=ROOT_STACK_RESOURCE),
            Alias(name="ol-live-offline-bucket-cors", parent=ROOT_STACK_RESOURCE),
            Alias(
                name="ol-live-offline-bucket-ownership-controls",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(name="ol-live-offline-bucket-versioning", parent=ROOT_STACK_RESOURCE),
            Alias(
                name="ol-live-offline-bucket-public-access", parent=ROOT_STACK_RESOURCE
            ),
            Alias(name="ol-live-offline-bucket-policy", parent=ROOT_STACK_RESOURCE),
        ],
        depends_on=[audit_log_bucket],
    ),
)

# test_offline bucket
test_offline_bucket_config = S3BucketConfig(
    bucket_name=test_offline_bucket_name,
    versioning_enabled=True,
    ownership_controls="BucketOwnerPreferred",
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
        )
    ],
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
    restrict_public_buckets=False,
    bucket_policy_document=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": [
                        f"{test_offline_bucket_arn}/*",
                    ],
                }
            ],
        }
    ),
    logging_target_bucket=audit_log_bucket_name,
    logging_target_prefix=f"ocw-site/{test_offline_bucket_name}/",
    logging_target_object_key_format=s3.BucketLoggingTargetObjectKeyFormatArgs(
        partitioned_prefix=s3.BucketLoggingTargetObjectKeyFormatPartitionedPrefixArgs(
            partition_date_source="EventTime"
        )
    ),
    logging_expected_bucket_owner=aws_account.account_id,
    intelligent_tiering_archive_access_days=None,  # S3 website, user-facing downloads
    intelligent_tiering_deep_archive_access_days=None,
    tags=aws_config.tags,
)

test_offline_bucket = OLBucket(
    "ocw-content-offline-test-bucket",
    config=test_offline_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(name=test_offline_bucket_name, parent=ROOT_STACK_RESOURCE),
            Alias(name="ol-test-offline-bucket-cors", parent=ROOT_STACK_RESOURCE),
            Alias(
                name="ol-test-offline-bucket-ownership-controls",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(name="ol-test-offline-bucket-versioning", parent=ROOT_STACK_RESOURCE),
            Alias(
                name="ol-test-offline-bucket-public-access", parent=ROOT_STACK_RESOURCE
            ),
            Alias(name="ol-test-offline-bucket-policy", parent=ROOT_STACK_RESOURCE),
        ],
        depends_on=[audit_log_bucket],
    ),
)

if stack_info.env_suffix == "production":
    def setup_bucket_replication(
        source_bucket: OLBucket,
        destination_bucket: OLBucket,
        *,
        source_bucket_name: str,
        source_bucket_arn: str,
        destination_bucket_arn: str,
        resource_prefix: str,
        role_name: str,
        policy_name: str,
        rule_id: str,
    ) -> s3.BucketReplicationConfig:
        """Create imported IAM and replication resources for a source/destination pair.

        Args:
            source_bucket: Source OCW bucket component.
            destination_bucket: Destination backup OCW bucket component.
            source_bucket_name: Existing source bucket name used for import IDs.
            source_bucket_arn: Source bucket ARN string used in linted IAM policy JSON.
            destination_bucket_arn: Destination bucket ARN string used in IAM policy JSON.
            resource_prefix: Pulumi logical resource name prefix.
            role_name: Existing replication IAM role name to import/manage.
            policy_name: Existing replication IAM policy name to import/manage.
            rule_id: Existing replication rule identifier.

        Returns:
            The imported BucketReplicationConfig resource for the source bucket.
        """
        policy_arn = (
            f"arn:aws:iam::{aws_account.account_id}:policy/service-role/{policy_name}"
        )
        replication_role = iam.Role(
            f"{resource_prefix}-replication-role",
            name=role_name,
            path="/service-role/",
            assume_role_policy=json.dumps(
                {
                    "Version": IAM_POLICY_VERSION,
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "s3.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                }
            ),
            opts=ResourceOptions(import_=role_name),
        )
        replication_policy = iam.Policy(
            f"{resource_prefix}-replication-policy",
            name=policy_name,
            path="/service-role/",
            policy=lint_iam_policy(
                {
                    "Version": IAM_POLICY_VERSION,
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "s3:ListBucket",
                                "s3:GetReplicationConfiguration",
                                "s3:GetObjectVersionForReplication",
                                "s3:GetObjectVersionAcl",
                                "s3:GetObjectVersionTagging",
                                "s3:GetObjectRetention",
                                "s3:GetObjectLegalHold",
                            ],
                            "Resource": [
                                source_bucket_arn,
                                f"{source_bucket_arn}/*",
                                destination_bucket_arn,
                                f"{destination_bucket_arn}/*",
                            ],
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "s3:ReplicateObject",
                                "s3:ReplicateDelete",
                                "s3:ReplicateTags",
                                "s3:ObjectOwnerOverrideToBucketOwner",
                            ],
                            "Resource": [
                                f"{source_bucket_arn}/*",
                                f"{destination_bucket_arn}/*",
                            ],
                        },
                    ],
                },
                stringify=True,
            ),
            opts=ResourceOptions(import_=policy_arn),
        )
        replication_policy_attachment = iam.RolePolicyAttachment(
            f"{resource_prefix}-replication-policy-attachment",
            role=replication_role.name,
            policy_arn=replication_policy.arn,
            opts=ResourceOptions(
                import_=f"{role_name}/{policy_arn}",
                depends_on=[
                    replication_role,
                    replication_policy,
                ],
            ),
        )

        return s3.BucketReplicationConfig(
            f"{resource_prefix}-replication",
            bucket=source_bucket.bucket_v2.id,
            role=replication_role.arn,
            rules=[
                s3.BucketReplicationConfigRuleArgs(
                    id=rule_id,
                    priority=0,
                    filter=s3.BucketReplicationConfigRuleFilterArgs(),
                    status="Enabled",
                    destination=s3.BucketReplicationConfigRuleDestinationArgs(
                        bucket=destination_bucket.bucket_v2.arn,
                    ),
                    delete_marker_replication=s3.BucketReplicationConfigRuleDeleteMarkerReplicationArgs(
                        status="Disabled"
                    ),
                )
            ],
            opts=ResourceOptions(
                import_=source_bucket_name,
                depends_on=[
                    resource
                    for resource in [
                        source_bucket.bucket_versioning,
                        destination_bucket.bucket_versioning,
                        replication_policy_attachment,
                    ]
                    if resource is not None
                ],
            ),
        )

    draft_bucket_replication = setup_bucket_replication(
        draft_bucket,
        draft_backup_bucket,
        source_bucket_name=draft_bucket_name,
        source_bucket_arn=draft_bucket_arn,
        destination_bucket_arn=draft_backup_bucket_arn,
        resource_prefix="ocw-content-draft-production",
        role_name="s3crr_role_for_ocw-content-draft-production",
        policy_name="s3crr_for_ocw-content-draft-production_8da254",
        rule_id="OCWContentDraftProductionBackupRule",
    )

    live_bucket_replication = setup_bucket_replication(
        live_bucket,
        live_backup_bucket,
        source_bucket_name=live_bucket_name,
        source_bucket_arn=live_bucket_arn,
        destination_bucket_arn=live_backup_bucket_arn,
        resource_prefix="ocw-content-live-production",
        role_name="s3crr_role_for_ocw-content-live-production",
        policy_name="s3crr_for_ocw-content-live-production_565f2a",
        rule_id="OCWContentLiveProductionBackupRule",
    )

policy_description = (
    "Access controls for the CDN to be able to read from the"
    f"{stack_info.env_suffix} website buckets"
)
s3_bucket_iam_policy = iam.Policy(
    f"ocw-site-{stack_info.env_suffix}-policy",
    description=policy_description,
    path=f"/ol-applications/ocw-site/{stack_info.env_suffix}/",
    name_prefix=f"ocw-site-content-read-only-{stack_info.env_suffix}",
    policy=lint_iam_policy(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:ListBucket*",
                        "s3:GetObject*",
                    ],
                    "Resource": [
                        draft_bucket_arn,
                        f"{draft_bucket_arn}/*",
                        test_bucket_arn,
                        f"{test_bucket_arn}/*",
                        live_bucket_arn,
                        f"{live_bucket_arn}/*",
                        draft_backup_bucket_arn,
                        f"{draft_backup_bucket_arn}/*",
                        draft_offline_bucket_arn,
                        f"{draft_offline_bucket_arn}/*",
                        live_bucket_arn,
                        f"{live_bucket_arn}/*",
                        live_backup_bucket_arn,
                        f"{live_backup_bucket_arn}/*",
                        live_offline_bucket_arn,
                        f"{live_offline_bucket_arn}/*",
                        test_offline_bucket_arn,
                        f"{test_offline_bucket_arn}/*",
                    ],
                }
            ],
        },
        stringify=True,
    ),
)

#################
# Fastly Config #
#################
site_domains = ocw_site_config.get_object("domains") or {"draft": [], "live": []}
fastly_shielding_enabled = ocw_site_config.get_bool("enable_fastly_shielding") or False
fastly_image_optimization_enabled = (
    ocw_site_config.get_bool("enable_fastly_image_optimization") or False
)
# Website Storage Bucket
website_storage_bucket_fqdn = "ocw-website-storage.s3.us-east-1.amazonaws.com"
project_dir = Path(__file__).resolve().parent
snippets_dir = project_dir.joinpath("snippets")
url_themes_404 = "https://raw.githubusercontent.com/mitodl/ocw-hugo-themes/release/www/layouts/404.html"
fastly_distributions: dict[str, fastly.ServiceVcl] = {}
for purpose in ("draft", "live", "test"):
    if stack_info.env_suffix == "production" and purpose == "live":
        robots_file = "robots.production.txt"
    else:
        robots_file = "robots.txt"

    website_bucket_fqdn = (
        f"ocw-content-{purpose}-{stack_info.env_suffix}.s3.us-east-1.amazonaws.com"
    )

    servicevcl_backend = fastly.ServiceVcl(
        f"ocw-{purpose}-{stack_info.env_suffix}",
        name=f"OCW {purpose.capitalize()} {stack_info.name}",
        backends=[
            fastly.ServiceVclBackendArgs(
                address=website_bucket_fqdn,
                name="WebsiteBucket",
                override_host=website_bucket_fqdn,
                port=DEFAULT_HTTPS_PORT,
                request_condition="not course media or old akamai",
                shield="iad-va-us" if fastly_shielding_enabled else None,
                ssl_cert_hostname=website_bucket_fqdn,
                ssl_sni_hostname=website_bucket_fqdn,
                use_ssl=True,
            ),
            fastly.ServiceVclBackendArgs(
                address=website_storage_bucket_fqdn,
                name="OCWWebsiteStorageBucket",
                override_host=website_storage_bucket_fqdn,
                port=DEFAULT_HTTPS_PORT,
                request_condition="is old Akamai file",
                shield="iad-va-us" if fastly_shielding_enabled else None,
                ssl_cert_hostname=website_storage_bucket_fqdn,
                ssl_sni_hostname=website_storage_bucket_fqdn,
                use_ssl=True,
            ),
        ],
        conditions=[
            fastly.ServiceVclConditionArgs(
                name="Generated by synthetic response for robots.txt",
                priority=0,
                statement='req.url.path == "/robots.txt"',
                type="REQUEST",
            ),
            fastly.ServiceVclConditionArgs(
                name="Generated by synthetic response for 404 page",
                statement="beresp.status == 404",
                type="CACHE",
            ),
            fastly.ServiceVclConditionArgs(
                name="not course media or old akamai",
                statement=(
                    'req.url.path !~ "^/coursemedia" && req.url.path !~ "^/ans\\d+"'
                ),
                type="REQUEST",
            ),
            fastly.ServiceVclConditionArgs(
                name="is old Akamai file",
                statement=(
                    'req.url.path ~ "^/ans\\d+" && req.url.path !~'
                    ' "/ans7870/21f/21f.027"'
                ),
                type="REQUEST",
            ),
        ],
        default_ttl=SECONDS_IN_ONE_DAY,
        dictionaries=[
            fastly.ServiceVclDictionaryArgs(
                name="redirects",
            )
        ],
        domains=[
            fastly.ServiceVclDomainArgs(name=domain) for domain in site_domains[purpose]
        ],
        gzips=[
            fastly.ServiceVclGzipArgs(
                content_types=[
                    "text/html",
                    "application/x-javascript",
                    "text/css",
                    "application/javascript",
                    "text/javascript",
                    "application/json",
                    "application/vnd.ms-fontobject",
                    "application/x-font-opentype",
                    "application/x-font-truetype",
                    "application/x-font-ttf",
                    "application/xml",
                    "font/eot",
                    "font/opentype",
                    "font/otf",
                    "image/svg+xml",
                    "image/vnd.microsoft.icon",
                    "text/plain",
                    "text/xml",
                ],
                extensions=[
                    "css",
                    "js",
                    "html",
                    "eot",
                    "ico",
                    "otf",
                    "ttf",
                    "json",
                    "svg",
                ],
                name="Generated by default gzip policy",
            )
        ],
        image_optimizer_default_settings=(
            fastly.ServiceVclImageOptimizerDefaultSettingsArgs(
                name="",
                webp=True,
            )
            if fastly_image_optimization_enabled
            else None
        ),
        product_enablement=fastly.ServiceVclProductEnablementArgs(
            brotli_compression=False,
            domain_inspector=False,
            image_optimizer=fastly_image_optimization_enabled,
            origin_inspector=False,
            websockets=False,
        ),
        headers=[
            fastly.ServiceVclHeaderArgs(
                action="set",
                destination="http.Access-Control-Allow-Origin",
                name="CORS Allow Star",
                source='"*"',
                type="cache",
            ),
            fastly.ServiceVclHeaderArgs(
                action="set",
                destination="http.Surrogate-Key",
                name="S3 Cache Surrogate Keys",
                priority=10,
                source="beresp.http.x-amz-meta-site-id",
                type="cache",
            ),
            fastly.ServiceVclHeaderArgs(
                action="set",
                destination="http.Strict-Transport-Security",
                name="Generated by force TLS and enable HSTS",
                source='"max-age=300"',
                type="response",
            ),
        ],
        request_settings=[
            fastly.ServiceVclRequestSettingArgs(
                force_ssl=True,
                name="Generated by force TLS and enable HSTS",
            )
        ],
        response_objects=[
            fastly.ServiceVclResponseObjectArgs(
                content=snippets_dir.joinpath(robots_file).read_text(),
                content_type="text/plain",
                name="Generated by synthetic response for robots.txt",
                request_condition="Generated by synthetic response for robots.txt",
                response="OK",
                status=HTTP_STATUS_OK,
            ),
            fastly.ServiceVclResponseObjectArgs(
                cache_condition="Generated by synthetic response for 404 page",
                content=httpx.get(url_themes_404).text,
                content_type="text/html",
                name="Generated by synthetic response for 404 page",
                response="Not Found",
                status=HTTP_STATUS_NOT_FOUND,
            ),
        ],
        snippets=[
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath("ttl_setup.vcl").read_text(),
                name="TTLs setup",
                priority=110,
                type="fetch",
            ),
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath("departments_table.vcl").read_text(),
                name="Departments Table",
                type="init",
            ),
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath("s3_bucket_proxying.vcl").read_text(),
                name="S3 Bucket Proxying",
                priority=200,
                type="miss",
            ),
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath("redirects.vcl").read_text(),
                name="Redirects",
                type="recv",
            ),
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath(
                    "legacy_ocw_pages_redirect.vcl"
                ).read_text(),
                name="Legacy OCW Pages Redirect",
                type="fetch",
            ),
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath(
                    "set_correct_content_type_for_S3_assets.vcl"
                ).read_text(),
                name="Set correct Content-type for S3 assets",
                type="fetch",
            ),
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath(
                    "clean_response_headers_and_handle_404_on_delivery.vcl"
                ).read_text(),
                name="Clean response headers and handle 404 on delivery",
                type="deliver",
            ),
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath("reroute_redirects.vcl").read_text(),
                name="Reroute Redirects",
                type="error",
            ),
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath(
                    "large_object_segmented_caching.vcl"
                ).read_text(),
                name="Segmented Caching",
                type="recv",
            ),
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath(
                    "strip_cookies_and_authorization_and_user_io.vcl"
                ).read_text(),
                name="Strip cookies and authorization and user provided io header.",
                type="recv",
            ),
            *(
                [
                    fastly.ServiceVclSnippetArgs(
                        content=snippets_dir.joinpath(
                            "image_optimization.vcl"
                        ).read_text(),
                        name="Image Optimization",
                        type="recv",
                        priority=1000,
                    )
                ]
                if fastly_image_optimization_enabled
                else []
            ),
        ],
        logging_https=[
            fastly.ServiceVclLoggingHttpArgs(
                url=Output.all(domain=vector_log_proxy_domain).apply(
                    lambda kwargs: f"https://{kwargs['domain']}/fastly"
                ),
                name=f"ocw-{purpose}-{stack_info.env_suffix}-https-logging-args",
                content_type="application/json",
                format=build_fastly_log_format_string(
                    additional_static_fields={
                        "application": "open-courseware",
                        "environment": f"ocw-{stack_info.env_suffix}",
                        # service will be applied by the vector-log-proxy
                    }
                ),
                format_version=2,
                header_name="Authorization",
                header_value=f"Basic {encoded_fastly_proxy_credentials}",
                json_format="0",
                method="POST",
                request_max_bytes=ONE_MEGABYTE_BYTE,
            ),
        ],
        logging_s3s=[
            fastly.ServiceVclLoggingS3Args(
                bucket_name=fastly_access_logging_bucket["bucket_name"],
                name=f"ocw-{purpose}-{stack_info.env_suffix}-s3-logging-args",
                format=build_fastly_log_format_string(additional_static_fields={}),
                gzip_level=3,
                message_type="blank",
                path=f"/ocw/{stack_info.env_suffix}/{purpose}/",
                redundancy="standard",
                s3_iam_role=fastly_access_logging_iam_role["role_arn"],
            ),
        ],
        stale_if_error=True,
        opts=ResourceOptions(
            protect=True,
        ).merge(fastly_provider),
    )

    fastly.ServiceDictionaryItems(
        f"ocw-{purpose}-{stack_info.env_suffix}",
        service_id=servicevcl_backend.id,
        dictionary_id=servicevcl_backend.dictionaries[0].dictionary_id,
        items=json.load(open("redirect_dict.json")),  # noqa: PTH123
        manage_items=True,
        opts=ResourceOptions(protect=True).merge(fastly_provider),
    )

    tls_configuration = fastly.get_tls_configuration(
        default=False,
        name="TLS v1.3",
        tls_protocols=["1.2", "1.3"],
        opts=InvokeOptions(provider=fastly_provider.provider),
    )

    fastly_tls = fastly.TlsSubscription(
        f"fastly-ocw_site-{stack_info.env_suffix}-{purpose}-tls-subscription",
        # valid values are certainly, lets-encrypt, or globalsign
        certificate_authority="certainly",
        domains=servicevcl_backend.domains.apply(
            lambda domains: [domain.name for domain in domains]
        ),
        # Retrieved from https://manage.fastly.com/network/tls-configurations
        configuration_id=tls_configuration.id,
        force_update=True,
        opts=fastly_provider,
    )

    fastly_tls.managed_dns_challenges.apply(fastly_certificate_validation_records)

    validated_tls_subscription = fastly.TlsSubscriptionValidation(
        f"{purpose}-tls-subscription-validation",
        subscription_id=fastly_tls.id,
        opts=fastly_provider,
    )

    fastly_distributions[purpose] = servicevcl_backend

    for domain in site_domains[purpose]:
        # If it's a 3 level domain then it's rooted at MIT.edu which means that we are
        # creating an Apex record in Route53. This means that we have to use an A
        # record. If it's deeper than 3 levels then it's a subdomain of ocw.mit.edu and
        # we can use a CNAME.
        record_type = "A" if len(domain.split(".")) == 3 else "CNAME"  # noqa: PLR2004
        route53.Record(
            f"ocw-site-dns-record-{domain}",
            name=domain,
            type=record_type,
            ttl=FIVE_MINUTES,
            records=[
                record.record_value
                for record in tls_configuration.dns_records
                if record.record_type == record_type
            ],
            zone_id=ocw_zone["id"],
        )

export(
    "ocw_site_buckets",
    {
        "buckets": {
            "draft": draft_bucket_name,
            "draft_backup": draft_backup_bucket_name,
            "draft_offline": draft_offline_bucket_name,
            "live": live_bucket_name,
            "live_backup": live_backup_bucket_name,
            "live_offline": live_offline_bucket_name,
        },
        "policy": s3_bucket_iam_policy.name,
        "fastly_draft_service_id": fastly_distributions["draft"].id,
        "fastly_live_service_id": fastly_distributions["live"].id,
    },
)
