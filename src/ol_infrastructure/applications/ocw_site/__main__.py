import json

from pulumi import Config, StackReference, export
from pulumi_aws import iam, route53, s3

from bridge.lib.constants import FASTLY_A_TLS_1_3, FASTLY_CNAME_TLS_1_3
from bridge.lib.magic_numbers import FIVE_MINUTES
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

dns_stack = StackReference("infrastructure.aws.dns")
ocw_zone = dns_stack.require_output("ocw")
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
draft_backup_bucket_arn = f"arn:aws:s3:::{draft_backup_bucket_name}"
live_backup_bucket_name = f"ocw-content-backup-live-{stack_info.env_suffix}"
live_backup_bucket_arn = f"arn:aws:s3:::{live_backup_bucket_name}"

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
    versioning=s3.BucketVersioningArgs(enabled=True),
    lifecycle_rules=[
        s3.BucketLifecycleRuleArgs(
            enabled=True,
            noncurrent_version_expiration=s3.BucketLifecycleRuleNoncurrentVersionExpirationArgs(
                days=90,
            ),
        )
    ],
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
    versioning=s3.BucketVersioningArgs(enabled=True),
    lifecycle_rules=[
        s3.BucketLifecycleRuleArgs(
            enabled=True,
            noncurrent_version_expiration=s3.BucketLifecycleRuleNoncurrentVersionExpirationArgs(
                days=90,
            ),
        )
    ],
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

# NOTE (TMM 2022-03-28): Once we integrate Fastly into this project code we'll likely
# want to turn this domain object into a dictionary of draft and live domains to be
# templated into the Fastly distribution as well.
for domain in ocw_site_config.get_object("domains") or []:
    # If it's a 3 level domain then it's rooted at MIT.edu which means that we are
    # creating an Apex record in Route53. This means that we have to use an A record. If
    # it's deeper than 3 levels then it's a subdomain of ocw.mit.edu and we can use a
    # CNAME.
    record_type = "A" if len(domain.split(".")) == 3 else "CNAME"
    record_value = FASTLY_A_TLS_1_3 if record_type == "A" else [FASTLY_CNAME_TLS_1_3]  # type: ignore[list-item]
    route53.Record(
        f"ocw-site-dns-record-{domain}",
        name=domain,
        type=record_type,
        ttl=FIVE_MINUTES,
        records=record_value,
        zone_id=ocw_zone["id"],
    )

export(
    "ocw_site_buckets",
    {
        "buckets": [
            draft_bucket_name,
            draft_backup_bucket_name,
            live_bucket_name,
            live_backup_bucket_name,
        ],
        "policy": s3_bucket_iam_policy.name,
    },
)
