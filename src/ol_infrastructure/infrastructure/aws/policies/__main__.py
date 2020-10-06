import json

from pulumi import export
from pulumi_aws import iam

describe_instance_policy_document = {
    'Version': '2012-10-17',
    'Statement': [
        {
            'Effect': 'Allow',
            'Action': 'ec2:DescribeInstances',
            'Resource': '*'
        }
    ]
}

describe_instance_policy = iam.Policy(
    'describe-ec2-instances-policy',
    name='describe-ec2-instances-policy',
    path='/ol-operations/describe-ec2-instances-policy/',
    policy=json.dumps(describe_instance_policy_document),
    description='Policy permitting EC2 describe instances capabilities for use with cloud auto-join systems.'
)

export('iam_policies', {
    'describe_instances': describe_instance_policy.arn
})
