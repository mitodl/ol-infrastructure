from pulumi import Config
from pulumi_aws import iam

from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.vault import setup_vault_provider

if Config("vault_server").get("env_namespace"):
    setup_vault_provider()

# Reference: https://grafana.com/docs/grafana/latest/datasources/aws-cloudwatch/
raw_policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "AllowReadingMetricsFromCloudWatch",
            "Effect": "Allow",
            "Action": [
                "cloudwatch:DescribeAlarmsForMetric",
                "cloudwatch:DescribeAlarmHistory",
                "cloudwatch:DescribeAlarms",
                "cloudwatch:ListMetrics",
                "cloudwatch:GetMetricData",
                "cloudwatch:GetInsightRuleReport",
            ],
            "Resource": "*",
        },
        {
            "Sid": "AllowReadingLogsFromCloudWatch",
            "Effect": "Allow",
            "Action": [
                "logs:DescribeLogGroups",
                "logs:GetLogGroupFields",
                "logs:StartQuery",
                "logs:StopQuery",
                "logs:GetQueryResults",
                "logs:GetLogEvents",
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
    policy=lint_iam_policy(
        raw_policy, parliament_config={"RESOURCE_STAR": {}}, stringify=True
    ),
)

grafana_user = iam.User("grafanacloud-readonly")

iam.UserPolicyAttachment(
    "grafana-readonly-cloudwatch-policy-attachment",
    user=grafana_user.name,
    policy_arn=grafana_cloudwatch_policy,
)
