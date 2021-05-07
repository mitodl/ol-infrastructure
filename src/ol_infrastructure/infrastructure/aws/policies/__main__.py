from pulumi import StackReference, export
from pulumi_aws import iam

from ol_infrastructure.lib.aws.iam_helper import (
    lint_iam_policy,
    route53_policy_template,
)

dns_stack = StackReference("infrastructure.aws.dns")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")

describe_instance_policy_document = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Action": "ec2:DescribeInstances", "Resource": "*"}
    ],
}
describe_instance_policy = iam.Policy(
    "describe-ec2-instances-policy",
    name="describe-ec2-instances-policy",
    path="/ol-operations/describe-ec2-instances-policy/",
    policy=lint_iam_policy(describe_instance_policy_document, stringify=True),
    description="Policy permitting EC2 describe instances capabilities for use with "
    "cloud auto-join systems.",
)

odl_zone_route53_policy = iam.Policy(
    "mitodl-zone-route53-records-policy",
    name="route53-odl-zone-records-policy",
    path="/ol-infrastructure/route53-odl-zone-records-policy/",
    policy=mitodl_zone_id.apply(
        lambda zone_id: lint_iam_policy(
            route53_policy_template(zone_id), stringify=True
        )
    ),
    description="Grant permissions to create Route53 records in the odl zone",
)

cloudwatch_logs_policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
                "logs:DescribeLogStreams",
            ],
            "Resource": ["arn:aws:logs:*:*:*"],
        }
    ],
}
create_cloudwatch_logs_policy = iam.Policy(
    "create-cloudwatch-log-group-policy",
    name="allow-cloudwatch-log-access",
    path="/ol-operations/global-policies/",
    policy=lint_iam_policy(cloudwatch_logs_policy, stringify=True),
)

export(
    "iam_policies",
    {
        "describe_instances": describe_instance_policy.arn,
        "cloudwatch_logging": create_cloudwatch_logs_policy.arn,
        "route53_odl_zone_records": odl_zone_route53_policy.arn,
    },
)
