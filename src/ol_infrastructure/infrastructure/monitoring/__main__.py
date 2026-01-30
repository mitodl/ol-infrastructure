"""Monitoring infrastructure resources including SNS topics and S3 buckets."""

from pulumi import (
    ROOT_STACK_RESOURCE,
    Alias,
    Config,
    ResourceOptions,
    StackReference,
    export,
)
from pulumi_aws import iam, sns

from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()

kms_stack = StackReference("infrastructure.aws.kms.Production")
kms_s3_key = kms_stack.require_output("kms_s3_data_analytics_key")

monitoring_config = Config("monitoring")

aws_config = AWSBase(tags={"OU": "operations", "Environment": "operations-production"})

### SNS Resources for notifying opsgenie

# TODO: MD 20230315 Migrate to SNS integration. Email is easier to start with.  # noqa: E501, FIX002, TD002
# https://support.atlassian.com/opsgenie/docs/integrate-opsgenie-with-incoming-amazon-sns/
critical_sns_topic = sns.Topic(
    "monitoring-critical-alerts-sns-topic",
    name="OpsGenie_Critical_Notifications",
    tags=aws_config.merged_tags({"Name": "OpsGenie Critical Notifications"}),
)
warning_sns_topic = sns.Topic(
    "monitoring-warning-alerts-sns-topic",
    name="OpsGenie_Warning_Notifications",
    tags=aws_config.merged_tags({"Name": "OpsGenie Warning Notifications"}),
)

critical_topic_subscription = sns.TopicSubscription(
    "monitoring-critical-alerts-sns-topic-subscription",
    endpoint=monitoring_config.get("opsgenie_critical_email_address"),
    protocol="email",
    topic=critical_sns_topic.arn,
)

warning_topic_subscription = sns.TopicSubscription(
    "monitoring-warning-alerts-sns-topic-subscription",
    endpoint=monitoring_config.get("opsgenie_warning_email_address"),
    protocol="email",
    topic=warning_sns_topic.arn,
)

export(
    "opsgenie_sns_topics",
    {
        "critical_sns_topic_arn": critical_sns_topic.arn,
        "warning_sns_topic_arn": warning_sns_topic.arn,
    },
)

### Fastly Logs shippting to S3
fastly_logging_bucket_name = monitoring_config.get("fastly_logging_bucket_name")

fastly_logging_bucket_config = S3BucketConfig(
    bucket_name=fastly_logging_bucket_name,
    versioning_enabled=False,
    ownership_controls="BucketOwnerEnforced",
    server_side_encryption_enabled=True,
    kms_key_id=kms_s3_key["id"],
    bucket_key_enabled=True,
    tags=aws_config.merged_tags(
        {"OU": "operations", "Name": fastly_logging_bucket_name}
    ),
)

fastly_logging_bucket = OLBucket(
    f"monitoring-{fastly_logging_bucket_name}",
    config=fastly_logging_bucket_config,
    opts=ResourceOptions(
        aliases=[
            # Old bucket resource aliases (existing)
            Alias(
                name=f"{fastly_logging_bucket_name}-production",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name=f"monitoring-{fastly_logging_bucket_name}-production",
                parent=ROOT_STACK_RESOURCE,
            ),
            # Current bucket resource alias
            Alias(
                name=f"monitoring-{fastly_logging_bucket_name}",
                parent=ROOT_STACK_RESOURCE,
            ),
            # Public access block aliases (existing)
            Alias(
                name=f"{fastly_logging_bucket_name}-production_block_public_access",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name=f"monitoring-{fastly_logging_bucket_name}-production_block_public_access",
                parent=ROOT_STACK_RESOURCE,
            ),
            # Current public access block alias
            Alias(
                name=f"monitoring-{fastly_logging_bucket_name}_block_public_access",
                parent=ROOT_STACK_RESOURCE,
            ),
            # Encryption alias (was inline)
            Alias(
                name=f"monitoring-{fastly_logging_bucket_name}-encryption",
                parent=ROOT_STACK_RESOURCE,
            ),
        ],
        protect=True,
    ),
)

# Ref: https://docs.fastly.com/en/guides/creating-an-aws-iam-role-for-fastly-logging
fastly_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:PutObject",
            ],
            "Resource": f"arn:aws:s3:::{fastly_logging_bucket_name}/*",
        }
    ],
}
fastly_logging_iam_policy_name = "ol-fastly-access-logs-policy"

fastly_logging_iam_policy = iam.Policy(
    fastly_logging_iam_policy_name,
    name=fastly_logging_iam_policy_name,
    path="/ol-infrastructure/iam/fastly/",
    policy=lint_iam_policy(fastly_policy_document, stringify=True),
)

fastly_logging_iam_role = iam.Role(
    "ol-fastly-access-logs-role",
    assume_role_policy={
        "Version": IAM_POLICY_VERSION,
        "Statement": {
            "Condition": {
                "StringEquals": {
                    "sts:ExternalId": monitoring_config.get("fastly_customer_id")
                },
            },
            "Effect": "Allow",
            "Action": "sts:AssumeRole",
            "Principal": {"AWS": "717331877981"},
            "Sid": "S3LoggingTrustPolicy",
        },
    },
)

iam.RolePolicyAttachment(
    "ol-fastly-access-logs-role-permissions",
    policy_arn=fastly_logging_iam_policy.arn,
    role=fastly_logging_iam_role.name,
)

export(
    "fastly_access_logging_bucket",
    {
        "bucket_name": fastly_logging_bucket_name,
        "bucket_arn": fastly_logging_bucket.bucket_v2.arn,
    },
)
export(
    "fastly_access_logging_iam_role",
    {
        "role_name": fastly_logging_iam_role.name,
        "role_arn": fastly_logging_iam_role.arn,
    },
)
