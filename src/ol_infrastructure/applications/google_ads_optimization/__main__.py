from pulumi_aws import get_caller_identity, iam

from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.lib.ol_types import AWSBase, BusinessUnit
from ol_infrastructure.lib.pulumi_helper import (
    parse_stack,
)
from ol_infrastructure.lib.vault import setup_vault_provider

# Parse stack and setup providers
stack_info = parse_stack()
setup_vault_provider(stack_info)
env_name = f"google-ads-opt-{stack_info.env_suffix}"

aws_account = get_caller_identity()

# No idea if we should use an existing OU or not.
aws_config = AWSBase(
    tags={"OU": BusinessUnit.mit_learn, "Environment": stack_info.env_suffix}
)

google_ads_opt_bucket_name = f"google-ads-opt-{stack_info.env_suffix}"
google_ads_opt_bucket_config = S3BucketConfig(
    bucket_name=google_ads_opt_bucket_name,
    versioning_enabled=True,
    bucket_policy_document=iam.get_policy_document(
        statements=[
            iam.GetPolicyDocumentStatementArgs(
                effect="Allow",
                principals=[
                    iam.GetPolicyDocumentStatementPrincipalArgs(
                        type="AWS",
                        identifiers=[f"arn:aws:iam::{aws_account.account_id}:root"],
                    )
                ],
                actions=["s3:*"],
                resources=[f"arn:aws:s3:::{google_ads_opt_bucket_name}/*"],
            )
        ]
    ).json,
    tags=aws_config.tags,
    region=aws_config.region,
)
google_ads_opt_bucket = OLBucket(
    f"google-ads-opt-bucket-{env_name}", config=google_ads_opt_bucket_config
)
