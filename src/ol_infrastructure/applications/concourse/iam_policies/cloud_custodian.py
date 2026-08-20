from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION

# AWS Permissions Document
# Allow infrastructure workers elevated permissions needed for running packer
policy_definition = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "autoscaling:DescribeAutoScalingGroups",
                "autoscaling:DescribeLaunchConfigurations",
                "ec2:CreateTags",
                "ec2:DeleteSnapshot",
                "ec2:DeleteVolume",
                "ec2:DeregisterImage",
                "ec2:DescribeImageAttribute",
                "ec2:DescribeImages",
                "ec2:DescribeInstanceStatus",
                "ec2:DescribeInstances",
                "ec2:DescribeLaunchTemplates",
                "ec2:DescribeLaunchTemplateVersions",
                "ec2:DescribeRegions",
                "ec2:DescribeSnapshots",
                "ec2:DescribeSubnets",
                "ec2:DescribeTags",
                "ec2:DescribeVolumes",
                "ec2:DetachVolume",
                "ec2:ModifyImageAttribute",
                "ec2:ModifyInstanceAttribute",
                "ec2:ModifySnapshotAttribute",
            ],
            "Resource": "*",
        },
        {
            # Cloud Custodian's `unused` filter on aws.security-group decides
            # whether a group is referenced by scanning the services that can
            # hold one -- see SGUsage.get_scanners() in c7n/resources/vpc.py,
            # which includes a "codebuild" scanner. The other scanners (ENIs,
            # SG cross-references, Lambda, launch configs, ECS/CloudWatch
            # Events rules) are already covered by this policy and by
            # iam_policies/infra.py; CodeBuild was not, so
            # tag-packer-sg-for-cleanup and perform-packer-sg-cleanup both
            # died on AccessDeniedException once the infra worker's
            # AdministratorAccess was detached.
            #
            # Both calls are required, not just the one in the error: c7n's
            # CodeBuildProject enumerates with
            # enum_spec = ('list_projects', ...) and then hydrates with
            # batch_detail_spec = ('batch_get_projects', ...).
            #
            # Resource "*" because the filter enumerates every project in the
            # account -- there is no narrower scope that still answers "is
            # this security group in use anywhere".
            "Effect": "Allow",
            "Action": [
                "codebuild:BatchGetProjects",
                "codebuild:ListProjects",
            ],
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "kms:Decrypt",
            ],
            "Resource": "*",
        },
    ],
}
