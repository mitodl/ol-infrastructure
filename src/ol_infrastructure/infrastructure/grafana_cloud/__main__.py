from pulumi_aws import iam

from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase

aws_config = AWSBase(tags={"OU": "operations", "Environment": "operations-production"})

# Reference: https://grafana.com/docs/grafana/latest/datasources/aws-cloudwatch/
raw_policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "AllowReadingMetricsFromCloudWatch",
            "Effect": "Allow",
            "Action": [
                "cloudwatch:DescribeAlarmsForMetric",
                "cloudwatch:ListMetrics",
                "cloudwatch:GetMetricData",
            ],
            "Resource": "*",
        },
        {
            "Sid": "AllowReadingAlarmsFromCloudWatch",
            "Effect": "Allow",
            "Action": [
                "cloudwatch:DescribeAlarmHistory",
                "cloudwatch:DescribeAlarms",
            ],
            "Resource": f"arn:*:cloudwatch:{aws_config.region}:*:alarm:*",
        },
        {
            "Sid": "AllowReadingInsightRuleReportsFromCloudWatch",
            "Effect": "Allow",
            "Action": [
                "cloudwatch:GetInsightRuleReport",
            ],
            "Resource": f"arn:*:cloudwatch:{aws_config.region}:*:insight-rule/*",
        },
        {
            "Sid": "AllowReadingLogsFromCloudWatch",
            "Effect": "Allow",
            "Action": [
                "logs:DescribeLogGroups",
                "logs:GetLogGroupFields",
                "logs:StartQuery",
                "logs:GetLogEvents",
            ],
            "Resource": "arn:*:logs:*:*:log-group:*",
        },
        {
            "Sid": "AllowStopLogQuery",
            "Effect": "Allow",
            "Action": [
                "logs:StopQuery",
            ],
            "Resource": "*",
        },
        {
            "Sid": "AllowGetLogQueryResults",
            "Effect": "Allow",
            "Action": [
                "logs:GetQueryResults",
            ],
            "Resource": "*",
        },
        {
            "Sid": "AllowReadingTagsInstancesRegionsFromEC2",
            "Effect": "Allow",
            "Action": [
                "ec2:DescribeTags",
                "ec2:DescribeInstances",
                "ec2:DescribeRegions",
            ],
            "Resource": "*",
        },
        {
            "Sid": "AllowReadingResourcesForTags",
            "Effect": "Allow",
            "Action": "tag:GetResources",
            "Resource": "*",
        },
    ],
}
grafana_cloudwatch_policy = iam.Policy(
    "create-grafana-readonly-cloudwatch-policy",
    name="allow-grafana-cloud-cloudwatch-access",
    path="/ol-operations/global-policies/",
    policy=lint_iam_policy(raw_policy, stringify=True),
    tags=aws_config.tags,
)

grafana_user = iam.User("grafanacloud-readonly", tags=aws_config.tags)

iam.UserPolicyAttachment(
    "grafana-readonly-cloudwatch-policy-attachment",
    user=grafana_user.name,
    policy_arn=grafana_cloudwatch_policy.arn,
)
