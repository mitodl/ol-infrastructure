from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION

# AWS Permissions Document
# This is the base policy that all concourse instances should have. Always include this.

policy_definition = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "ec2:DescribeInstances",
                "ec2:DescribeInstanceStatus",
                "cloudwatch:PutMetricData",
            ],
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "autoscaling:DescribeAutoScalingInstances",
                "autoscaling:DescribeLifecycleHooks",
            ],
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "autoscaling:CompleteLifecycleAction",
                "autoscaling:SetInstanceHealth",
            ],
            "Resource": "arn:aws:autoscaling:*:*:autoScalingGroup:*:autoScalingGroupName/concourse-worker-*",  # noqa: E501
        },
    ],
}
