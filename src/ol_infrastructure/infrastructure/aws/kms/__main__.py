"""State to create symmetric AWS KMS Customer Master Keys (CMK) for use
with EC2 volume encryption. 
"""
import json
from pulumi import get_stack
from pulumi_aws import get_caller_identity, kms


owner = str(get_caller_identity().account_id)
stack = get_stack()
stack_name = stack.split('.')[-1]
env_suffix = stack_name.lower()
kms_ec2_environment = f'ec2-ebs-{env_suffix}'

kms_ec2_ebs_encryption_policy = {
    'Version': '2012-10-17',
    'Id': 'auto-ebs-2',
    'Statement': [
        {
            'Sid': 'Allow access through EBS for all principals in the account that are authorized to use EBS',
            'Effect': 'Allow',
            'Principal': {
                'AWS': '*'
            },
            'Action': [
                'kms:Encrypt',
                'kms:Decrypt',
                'kms:ReEncrypt*',
                'kms:GenerateDataKey*',
                'kms:CreateGrant',
                'kms:DescribeKey'
            ],
            'Resource': '*',
            'Condition': {
                'StringEquals': {
                    'kms:ViaService': 'ec2.us-east-1.amazonaws.com',
                    'kms:CallerAccount': f'{owner}'
                }
            }
        },
        {
            'Sid': 'Allow direct access to key metadata to the account',
            'Effect': 'Allow',
            'Principal': {
                'AWS': [
                  f'arn:aws:iam::{owner}:root',
                  f'arn:aws:iam::{owner}:user/mbreedlove',
                  f'arn:aws:iam::{owner}:user/shaidar',
                  f'arn:aws:iam::{owner}:user/tmacey',
                ]
            },
            'Action': [
                 'kms:Create*',
	             'kms:Describe*',
	             'kms:Enable*',
	             'kms:List*',
	             'kms:Put*',
	             'kms:Update*',
	             'kms:Revoke*',
	             'kms:Disable*',
	             'kms:Get*',
	             'kms:Delete*',
	             'kms:TagResource',
	             'kms:UnTagResource',
	             'kms:ScheduleKeyDeletion',
	             'kms:CancelKeyDeletion'
            ],
            'Resource': '*'
        }
    ]
}

key = kms.Key(kms_ec2_environment,
	customer_master_key_spec='SYMMETRIC_DEFAULT',
	description=f"Key to encrypt/decrypt {env_suffix} EBS volumes",
	enable_key_rotation=True,
	is_enabled=True,
	key_usage='ENCRYPT_DECRYPT',
	policy=json.dumps(kms_ec2_ebs_encryption_policy),
	tags={
	    'OU': 'operations',
	    'Environment': f'operations-{env_suffix}'
	},
)
