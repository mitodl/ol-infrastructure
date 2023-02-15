from pulumi import StackReference, export, Config
from pulumi_aws import s3, iam

from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy

stack_info = parse_stack()

kms_stack = StackReference(f"infrastructure.aws.kms.{stack_info.name}")
kms_s3_key = kms_stack.require_output("kms_s3_data_analytics_key")

monitoring_config = Config("monitoring")

fastly_logging_bucket_name = monitoring_config.get("fastly_logging_bucket_name")

fastly_logging_bucket = s3.Bucket(
    fastly_logging_bucket_name,
    bucket=fastly_logging_bucket_name,
    acl="private",
    server_side_encryption_configuration=s3.BucketServerSideEncryptionConfigurationArgs(
        rule=s3.BucketServerSideEncryptionConfigurationRuleArgs(
            apply_server_side_encryption_by_default=s3.BucketServerSideEncryptionConfigurationRuleApplyServerSideEncryptionByDefaultArgs(  # noqa: E501
                sse_algorithm="aws:kms",
                kms_master_key_id=kms_s3_key["id"],
            ),
            bucket_key_enabled=True,
        )
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
fastly_logging_iam_policy_name = "ol-fast-access-logs-policy"

fastly_logging_iam_policy = iam.Policy(
    fastly_logging_iam_policy_name,
    name="access-logs-policy",
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
