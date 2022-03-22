import json

from pulumi import Config, export
from pulumi_aws import iam, s3

from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

ocw_site_config = Config("ocw_site")
stack_info = parse_stack()
aws_config = AWSBase(
    tags={
        "OU": "open-courseware",
        "Environment": f"applications_{stack_info.env_suffix}",
    }
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

draft_backup_bucket_name = f"ocw-content-backup-draft-{stack_info.env_suffix}"
draft_backup_bucket_arn = f"arn:aws:s3:::{draft_bucket_name}"
live_backup_bucket_name = f"ocw-content-backup-live-{stack_info.env_suffix}"
live_backup_bucket_arn = f"arn:aws:s3:::{live_bucket_name}"

draft_bucket = s3.Bucket(
    draft_bucket_name,
    bucket=draft_bucket_name,
    tags=aws_config.tags,
    acl="public-read",
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"{draft_bucket_arn}/*"],
                }
            ],
        }
    ),
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
)
draft_backup_bucket = s3.Bucket(
    draft_backup_bucket_name,
    bucket=draft_backup_bucket_name,
    tags=aws_config.tags,
    acl="public-read",
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"{draft_backup_bucket_arn}/*"],
                }
            ],
        }
    ),
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
)

live_bucket = s3.Bucket(
    live_bucket_name,
    bucket=live_bucket_name,
    tags=aws_config.tags,
    acl="public-read",
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"{live_bucket_arn}/*"],
                }
            ],
        }
    ),
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
)
live_backup_bucket = s3.Bucket(
    live_backup_bucket_name,
    bucket=live_backup_bucket_name,
    tags=aws_config.tags,
    acl="public-read",
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"{live_backup_bucket_arn}/*"],
                }
            ],
        }
    ),
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
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
                        draft_backup_bucket_arn,
                        f"{draft_backup_bucket_arn}/*",
                        live_bucket_arn,
                        f"{live_bucket_arn}/*",
                        live_backup_bucket_arn,
                        f"{live_backup_bucket_arn}/*",
                    ],
                }
            ],
        },
        stringify=True,
    ),
)

export(
    "ocw_site_buckets",
    {
        "buckets": [
            draft_bucket_name,
            draft_backup_bucket_name,
            live_bucket_name,
            live_backup_bucket,
        ],
        "policy": s3_bucket_iam_policy.name,
    },
)
