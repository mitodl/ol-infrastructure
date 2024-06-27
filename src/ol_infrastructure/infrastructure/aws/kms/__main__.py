"""State to create symmetric AWS KMS Customer Master Keys (CMK)."""

import json

from pulumi import export
from pulumi_aws import get_caller_identity, kms

from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

DEFAULT_KEY_SPEC = "SYMMETRIC_DEFAULT"
ENCRYPT_KEY_USAGE = "ENCRYPT_DECRYPT"

owner = str(get_caller_identity().account_id)
stack_info = parse_stack()
kms_ec2_environment = f"ec2-ebs-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={"OU": "operations", "Environment": f"operations-{stack_info.env_suffix}"}
)
ANY_RESOURCE = "*"

kms_key_root_statement = {
    "Effect": "Allow",
    "Principal": {"AWS": f"arn:aws:iam::{owner}:root"},
    "Action": "kms:*",
    "Resource": "*",
}

kms_key_access_statement = {
    # Allow direct access to key metadata to the account
    "Effect": "Allow",
    "Principal": {
        "AWS": [
            f"arn:aws:iam::{owner}:user/shaidar",
            f"arn:aws:iam::{owner}:user/tmacey",
        ]
    },
    "Action": [
        "kms:Create*",
        "kms:Describe*",
        "kms:Enable*",
        "kms:List*",
        "kms:Put*",
        "kms:Update*",
        "kms:Revoke*",
        "kms:Disable*",
        "kms:Get*",
        "kms:Delete*",
        "kms:TagResource",
        "kms:UnTagResource",
        "kms:ScheduleKeyDeletion",
        "kms:CancelKeyDeletion",
    ],
    "Resource": ANY_RESOURCE,
}

kms_ec2_ebs_encryption_policy = {
    "Version": IAM_POLICY_VERSION,
    "Id": "auto-ebs-2",
    "Statement": [
        {
            # Allow access through EBS for all principals in the account that are
            # authorized to use EBS
            "Effect": "Allow",
            "Principal": {"AWS": ANY_RESOURCE},
            "Action": [
                "kms:Encrypt",
                "kms:Decrypt",
                "kms:ReEncrypt*",
                "kms:GenerateDataKey*",
                "kms:CreateGrant",
                "kms:DescribeKey",
            ],
            "Resource": ANY_RESOURCE,
            "Condition": {
                "StringEquals": {
                    "kms:ViaService": "ec2.us-east-1.amazonaws.com",
                    "kms:CallerAccount": f"{owner}",
                }
            },
        },
        kms_key_access_statement,
        kms_key_root_statement,
    ],
}

kms_s3_data_encryption_policy = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        kms_key_root_statement,
        kms_key_access_statement,
        {
            "Effect": "Allow",
            "Principal": {"AWS": ANY_RESOURCE},
            "Action": [
                "kms:Encrypt",
                "kms:Decrypt",
                "kms:ReEncrypt*",
                "kms:GenerateDataKey*",
                "kms:CreateGrant",
                "kms:DescribeKey",
            ],
            "Resource": ANY_RESOURCE,
            "Condition": {
                "StringEquals": {
                    "kms:ViaService": "s3.us-east-1.amazonaws.com",
                    "kms:CallerAccount": f"{owner}",
                }
            },
        },
    ],
}

kms_infrastructure_as_code_encryption_policy = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        kms_key_root_statement,
        kms_key_access_statement,
        {
            "Effect": "Allow",
            "Principal": {
                "AWS": [
                    f"arn:aws:iam::{owner}:user/tmacey",
                    f"arn:aws:iam::{owner}:user/shaidar",
                ]
            },
            "Action": [
                "kms:Encrypt",
                "kms:Decrypt",
                "kms:ReEncrypt*",
                "kms:GenerateDataKey*",
                "kms:DescribeKey",
            ],
            "Resource": ANY_RESOURCE,
        },
    ],
}

ebs_key = kms.Key(
    kms_ec2_environment,
    customer_master_key_spec=DEFAULT_KEY_SPEC,
    description=f"Key to encrypt/decrypt {stack_info.env_suffix} EBS volumes",
    enable_key_rotation=True,
    is_enabled=True,
    key_usage=ENCRYPT_KEY_USAGE,
    policy=json.dumps(kms_ec2_ebs_encryption_policy),
    tags=aws_config.merged_tags({"Name": kms_ec2_environment}),
)
ebs_key_alias = kms.Alias(
    f"{kms_ec2_environment}-alias",
    name=f"alias/{kms_ec2_environment}",
    target_key_id=ebs_key.id,
)
export(
    "kms_ec2_ebs_key",
    {"id": ebs_key.id, "arn": ebs_key.arn, "alias": ebs_key_alias.name},
)

# KMS key used for encrypting data in S3 used for data analytics/warehousing
s3_data_key = kms.Key(
    f"s3-data-analytics-{stack_info.env_suffix}",
    customer_master_key_spec=DEFAULT_KEY_SPEC,
    description=(
        "Key for encrypting data in S3 buckets used for "
        f"analytical/warehousing purposes in {stack_info.env_suffix} environments"
    ),
    enable_key_rotation=True,
    is_enabled=True,
    key_usage=ENCRYPT_KEY_USAGE,
    tags=AWSBase(
        tags={
            "OU": "data",
            "Environment": f"data-{stack_info.env_suffix}",
            "Owner": "platform-engineering",
        }
    ).tags,
    policy=json.dumps(kms_s3_data_encryption_policy),
)
s3_data_key_alias = kms.Alias(
    f"s3-data-analytics-{stack_info.env_suffix}-alias",
    name=f"alias/s3-analytical-data-{stack_info.env_suffix}",
    target_key_id=s3_data_key.id,
)

export(
    "kms_s3_data_analytics_key",
    {"id": s3_data_key.id, "arn": s3_data_key.arn, "alias": s3_data_key_alias.name},
)

# KMS key used for encrypting secrets managed with SOPS
infrastructure_as_code_key = kms.Key(
    f"infrastructure-secret-management-key-{stack_info.env_suffix}",
    customer_master_key_spec=DEFAULT_KEY_SPEC,
    description=(
        "Key for encrypting secrets used in building and deploying infrastructure "
        f"in {stack_info.env_suffix} environments."
    ),
    enable_key_rotation=True,
    is_enabled=True,
    key_usage=ENCRYPT_KEY_USAGE,
    tags=AWSBase(
        tags={
            "OU": "operations",
            "Environment": f"operations-{stack_info.env_suffix}",
            "Owner": "platform-engineering",
        }
    ).tags,
    policy=json.dumps(kms_infrastructure_as_code_encryption_policy),
)
infrastructure_as_code_key_alias = kms.Alias(
    f"infrastructure-secret-management-key-{stack_info.env_suffix}-alias",
    name=f"alias/infrastructure-secrets-{stack_info.env_suffix}",
    target_key_id=infrastructure_as_code_key.id,
)

export(
    "sops_infrastructure_secrets_key",
    {
        "id": infrastructure_as_code_key.id,
        "arn": infrastructure_as_code_key.arn,
        "alias": infrastructure_as_code_key_alias.name,
    },
)

vault_server_unseal_key = kms.Key(
    "vault-server-auto-unseal-kms-key",
    customer_master_key_spec=DEFAULT_KEY_SPEC,
    description=(
        "Key for automatically initializing and unsealing Vault servers in an "
        "autoscale group"
    ),
    enable_key_rotation=True,
    is_enabled=True,
    key_usage=ENCRYPT_KEY_USAGE,
    tags=AWSBase(
        tags={
            "OU": "operations",
            "Environment": f"operations-{stack_info.env_suffix}",
            "Owner": "platform-engineering",
        }
    ).tags,
    policy=json.dumps(kms_infrastructure_as_code_encryption_policy),
)
vault_server_unseal_key_alias = kms.Alias(
    "vault-server-auto-unseal-kms-key-alias",
    name=f"alias/vault-auto-unseal-{stack_info.env_suffix}",
    target_key_id=vault_server_unseal_key.id,
)

export(
    "vault_auto_unseal_key",
    {
        "id": vault_server_unseal_key.id,
        "arn": vault_server_unseal_key.arn,
        "alias": vault_server_unseal_key_alias.name,
    },
)
