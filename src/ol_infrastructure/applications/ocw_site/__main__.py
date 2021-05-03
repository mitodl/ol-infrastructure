from pulumi import Config, export
from pulumi_aws import iam, s3
from pulumi_vault import aws

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
live_bucket_name = f"ocw-content-live-{stack_info.env_suffix}"

draft_bucket = s3.Bucket(
    draft_bucket_name,
    bucket=draft_bucket_name,
    tags=aws_config.tags,
)

live_bucket = s3.Bucket(
    live_bucket_name,
    bucket=live_bucket_name,
    tags=aws_config.tags,
)

policy_description = (
    "Access controls for the CDN to be able to read from the"
    f"{stack_info.env_suffix} website buckets"
)
s3_bucket_iam_policy = iam.Policy(
    f"ocw-site-{stack_info.env_suffix}-policy",
    description=policy_description,
    path=f"/ol-applications/ocw-site/{stack_info.env_suffix}/",
    name_prefix="aws-permissions-",
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
                        f"arn:aws:s3:::{draft_bucket_name}",
                        f"arn:aws:s3:::{draft_bucket_name}/*",
                        f"arn:aws:s3:::{live_bucket_name}",
                        f"arn:aws:s3:::{live_bucket_name}/*",
                    ],
                }
            ],
        },
        stringify=True,
    ),
)

role_name = f"ocw-site-{stack_info.env_suffix}"
ocw_site_vault_backend_role = aws.SecretBackendRole(
    role_name,
    name=role_name,
    backend="aws-mitx",
    credential_type="iam_user",
    policy_arns=[s3_bucket_iam_policy.arn],
)

export("ocw_site_buckets", {"buckets": [draft_bucket_name, live_bucket_name]})
