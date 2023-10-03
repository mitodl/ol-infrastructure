from pulumi import Alias, Config, ResourceOptions, StackReference, export
from pulumi_aws import iam, s3, sns

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

fastly_logging_bucket = s3.Bucket(
    f"monitoring-{fastly_logging_bucket_name}",
    bucket=fastly_logging_bucket_name,
    acl="private",
    server_side_encryption_configuration=s3.BucketServerSideEncryptionConfigurationArgs(
        rule=s3.BucketServerSideEncryptionConfigurationRuleArgs(
            apply_server_side_encryption_by_default=s3.BucketServerSideEncryptionConfigurationRuleApplyServerSideEncryptionByDefaultArgs(
                sse_algorithm="aws:kms",
                kms_master_key_id=kms_s3_key["id"],
            ),
            bucket_key_enabled=True,
        )
    ),
    tags=aws_config.merged_tags(
        {"OU": "operations", "Name": fastly_logging_bucket_name}
    ),
    # Renamed this resource but we don't want pulumi to destroy the existing bucket.
    opts=ResourceOptions(
        aliases=[
            Alias(name=f"{fastly_logging_bucket_name}-production"),
            Alias(name=f"monitoring-{fastly_logging_bucket_name}-production"),
        ],
        protect=True,
    ),
)

s3.BucketPublicAccessBlock(
    f"monitoring-{fastly_logging_bucket_name}_block_public_access",
    bucket=fastly_logging_bucket.bucket,
    block_public_acls=True,
    block_public_policy=True,
    # Renamed this resource but we don't want pulumi to destroy the
    # existing configuration
    opts=ResourceOptions(
        aliases=[
            Alias(name=f"{fastly_logging_bucket_name}-production_block_public_access"),
            Alias(
                name=f"monitoring-{fastly_logging_bucket_name}-production_block_public_access",
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
        "bucket_arn": fastly_logging_bucket.arn,
    },
)
export(
    "fastly_access_logging_iam_role",
    {
        "role_name": fastly_logging_iam_role.name,
        "role_arn": fastly_logging_iam_role.arn,
    },
)
