"""The complete state needed to provision OVS running on Docker.

"""

import json

from pulumi_aws import s3

from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()
aws_config = AWSBase(
    tags={
        "OU": "odl-video",
        "Environment": f"applications_{stack_info.env_suffix}",
    }
)

static_assets_bucket_name = f"ovs-static-assets-{stack_info.env_suffix}"
static_assets_bucket_arn = f"arn:aws:s3:::{static_assets_bucket_name}"

static_assets_bucket = s3.Bucket(
    "static_assets_bucket",
    bucket=static_assets_bucket_name,
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
                    "Resource": [f"{static_assets_bucket_arn}/*"],
                }
            ],
        }
    ),
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
)
