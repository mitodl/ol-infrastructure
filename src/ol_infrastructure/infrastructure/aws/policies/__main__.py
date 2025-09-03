from pulumi import StackReference, export
from pulumi_aws import iam

from ol_infrastructure.lib.aws.iam_helper import (
    lint_iam_policy,
    route53_policy_template,
)

dns_stack = StackReference("infrastructure.aws.dns")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")

default_instance_policy_document = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "ec2:DescribeInstances",
                "ecr:CreateRepository",
                "ecr:BatchImportUpstreamImage",
                "ecr:GetAuthorizationToken",
            ],
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "ecr:BatchCheckLayerAvailability",
                "ecr:GetDownloadUrlForLayer",
                "ecr:GetRepositoryPolicy",
                "ecr:DescribeRepositories",
                "ecr:ListImages",
                "ecr:DescribeImages",
                "ecr:BatchGetImage",
                "ecr:GetLifecyclePolicy",
                "ecr:GetLifecyclePolicyPreview",
                "ecr:ListTagsForResource",
                "ecr:DescribeImageScanFindings",
            ],
            "Resource": ["arn:aws:ecr:*:*:repository/*"],
        },
    ],
}
default_instance_policy = iam.Policy(
    "describe-ec2-instances-policy",
    name="describe-ec2-instances-policy",
    path="/ol-operations/describe-ec2-instances-policy/",
    policy=lint_iam_policy(
        default_instance_policy_document,
        stringify=True,
        parliament_config={
            "CREDENTIALS_EXPOSURE": {
                "ignore_locations": [{"actions": ["ecr:GetAuthorizationToken"]}]
            },
        },
    ),
    description=(
        "Policy permitting EC2 describe instances capabilities for use with "
        "cloud auto-join systems."
    ),
)

# TODO MAD 20221208  # noqa: FIX002, TD002, TD004
# Leave this to not break compatibility with other stacks importing this
# Could probably be intergated with app_route_53 zone loop with a tweak
# the output of the infrastructure.aws.dns stack
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

app_route53_zones = ["mitx", "mitxonline", "xpro", "ocw", "ol", "learn"]
app_route53_policies = {}
for zone in app_route53_zones:
    zone_id = dns_stack.require_output(zone)["id"]
    policy = iam.Policy(
        f"{zone}-zone-route53-records-policy",
        name=f"route53-{zone}-zone-records-policy",
        path=f"/ol-infrastructure/route53-{zone}-zone-records-policy/",
        policy=zone_id.apply(
            lambda z_id: lint_iam_policy(route53_policy_template(z_id), stringify=True)
        ),
        description=f"Grant permissions to create Route53 records in the {zone} zone",
    )
    app_route53_policies[f"route53_{zone}_zone_records"] = policy.arn


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


export_dict = {
    "describe_instances": default_instance_policy.arn,
    "cloudwatch_logging": create_cloudwatch_logs_policy.arn,
    "route53_odl_zone_records": odl_zone_route53_policy.arn,
} | app_route53_policies

export("iam_policies", export_dict)
