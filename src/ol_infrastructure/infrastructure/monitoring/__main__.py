"""Monitoring infrastructure resources including SNS topics and S3 buckets."""

from pulumi import (
    ROOT_STACK_RESOURCE,
    Alias,
    Config,
    ResourceOptions,
    export,
)
from pulumi_aws import iam, sns

from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import (
    make_stack_reference,
    parse_stack,
)

stack_info = parse_stack()

kms_stack = make_stack_reference(projects.KMS, "Production")
kms_s3_key = kms_stack.require_output("kms_s3_data_analytics_key")

monitoring_config = Config("monitoring")

aws_config = AWSBase(tags={"OU": "operations", "Environment": "operations-production"})

### SNS resources for severity-based alert notifications
#
# Phase 6 of https://github.com/mitodl/ol-infrastructure/issues/4828: these
# topics are named `OpsGenie_*` because the org actually used OpsGenie when
# they were created; the name was never updated after migrating to Rootly,
# so it's stale history rather than a typo. Renaming an sns.Topic forces
# replacement (new ARN), and every consumer across 16+ downstream app stacks
# resolves the ARN dynamically via get_monitoring_sns_arn()'s StackReference
# lookup rather than a hardcoded name/ARN, so deleting the old topics
# immediately would silently break CloudWatch alarm notifications for any
# stack not yet redeployed.
#
# So this adds new topics alongside the old ones (not a rename in place) and
# repoints the shared export at them. Downstream stacks pick up the new ARN
# the next time they redeploy on their own schedule -- no code changes
# needed on their end, since none of them reference the topic by literal
# name. The old OpsGenie_* topics are intentionally left defined here and
# NOT deleted yet; do that as a separate follow-up once a live AWS check
# (CloudWatch alarms' AlarmActions across all accounts/regions) confirms
# nothing still references them.
#
# Named after the severity tier, not the destination service, so the next
# notification-service change doesn't leave another stale vendor name behind.
critical_sns_topic = sns.Topic(
    "monitoring-critical-alerts-sns-topic",
    name="OpsGenie_Critical_Notifications",
    tags=aws_config.merged_tags({"Name": "Rootly Critical Notifications"}),
)
warning_sns_topic = sns.Topic(
    "monitoring-warning-alerts-sns-topic",
    name="OpsGenie_Warning_Notifications",
    tags=aws_config.merged_tags({"Name": "Rootly Warning Notifications"}),
)

critical_top_webhook_subscription = sns.TopicSubscription(
    "monitoring-critical-alerts-sns-topic-webhook-subscription",
    endpoint=monitoring_config.get("rootly_critical_webhook_url"),
    protocol="https",
    topic=critical_sns_topic.arn,
)

warning_topic_subscription = sns.TopicSubscription(
    "monitoring-warning-alerts-sns-topic-subscription",
    endpoint=monitoring_config.get("rootly_warning_webhook_url"),
    protocol="https",
    topic=warning_sns_topic.arn,
)

critical_notifications_sns_topic = sns.Topic(
    "monitoring-critical-notifications-sns-topic",
    name="Critical_Notifications",
    tags=aws_config.merged_tags({"Name": "Critical Notifications"}),
)
warning_notifications_sns_topic = sns.Topic(
    "monitoring-warning-notifications-sns-topic",
    name="Warning_Notifications",
    tags=aws_config.merged_tags({"Name": "Warning Notifications"}),
)

critical_notifications_webhook_subscription = sns.TopicSubscription(
    "monitoring-critical-notifications-sns-topic-webhook-subscription",
    endpoint=monitoring_config.get("rootly_critical_webhook_url"),
    protocol="https",
    topic=critical_notifications_sns_topic.arn,
)

warning_notifications_webhook_subscription = sns.TopicSubscription(
    "monitoring-warning-notifications-sns-topic-webhook-subscription",
    endpoint=monitoring_config.get("rootly_warning_webhook_url"),
    protocol="https",
    topic=warning_notifications_sns_topic.arn,
)

# Old export retained so any not-yet-redeployed downstream stack's
# StackReference read still resolves during the transition; get_monitoring_sns_arn()
# (lib/aws/monitoring_helper.py) falls back to this if notification_sns_topics
# is absent. A repo-wide grep for "opsgenie_sns_topics" is NOT a sufficient
# removal criterion by itself -- it only reflects checked-out code, not
# deployed state. A stack can have zero code references to the old export
# while its last-deployed CloudWatch alarms still have the old topic ARN
# baked in, simply because it hasn't been redeployed since this change
# merged. Before removing this export (and, later, the old topics
# themselves), confirm via a live AWS check -- e.g. `aws cloudwatch
# describe-alarms` across all accounts/regions, filtered for AlarmActions
# matching the old OpsGenie_* topic ARNs -- that nothing still points at
# them, not just that the source code has moved on.
export(
    "opsgenie_sns_topics",
    {
        "critical_sns_topic_arn": critical_sns_topic.arn,
        "warning_sns_topic_arn": warning_sns_topic.arn,
    },
)
export(
    "notification_sns_topics",
    {
        "critical_sns_topic_arn": critical_notifications_sns_topic.arn,
        "warning_sns_topic_arn": warning_notifications_sns_topic.arn,
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
