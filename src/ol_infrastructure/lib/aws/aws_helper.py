import pulumi_aws as aws

AWS_ACCOUNT_ID = aws.get_caller_identity().account_id
