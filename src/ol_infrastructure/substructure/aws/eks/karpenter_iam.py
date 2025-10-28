# ruff: noqa: E501, ERA001

"""
Karpenter IAM policy for the cluster.
This policy is scoped to the cluster and allows Karpenter to create
and manage EC2 instances, launch templates, and other resources
necessary for autoscaling.
"""

# A lot of this is commented out from my troubleshooting permissions errors.
# This is where I stopped once things started working.
# Retaining this for posterity though, since it is derived from the official
# karpenter documentation / IAM policy / cloudformation code .
#
# cloudformation -- gross. who even uses that?

from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION


def get_cluster_karpenter_iam_policy_document(  # noqa: PLR0913
    aws_partition: str,
    aws_region: str,
    aws_account_id: str,
    cluster_name: str,
    karpenter_interruption_queue_arn: str,
    karpenter_node_role_arn: str,
):
    """
    Generate the Karpenter IAM policy for the cluster.

    This policy is scoped to the cluster and allows Karpenter to create
    and manage EC2 instances, launch templates, and other resources
    necessary for autoscaling.
    """
    return {
        "Version": IAM_POLICY_VERSION,
        "Statement": [
            {
                "Sid": "AllowScopedEC2InstanceAccessActions",
                "Effect": "Allow",
                "Resource": ["*"],
                #                    f"arn:{aws_partition}:ec2:{aws_region}::image/*",
                #                    f"arn:{aws_partition}:ec2:{aws_region}::snapshot/*",
                #                    f"arn:{aws_partition}:ec2:{aws_region}:*:security-group/*",
                #                    f"arn:{aws_partition}:ec2:{aws_region}:*:subnet/*",
                #                    f"arn:{aws_partition}:ec2:{aws_region}:*:capacity-reservation/*",
                #               ],
                "Action": ["ec2:RunInstances", "ec2:CreateFleet"],
            },
            {
                "Sid": "AllowScopedEC2LaunchTemplateAccessActions",
                "Effect": "Allow",
                "Resource": [
                    "*"
                ],  # f"arn:{aws_partition}:ec2:{aws_region}:*:launch-template/*",
                "Action": ["ec2:RunInstances", "ec2:CreateFleet"],
                #                "Condition": {
                #                    "StringEquals": {
                #                        f"aws:ResourceTag/kubernetes.io/cluster/{cluster_name}": "owned"
                #                    },
                #                    "StringLike": {"aws:ResourceTag/karpenter.sh/nodepool": "*"},
                #                },
            },
            {
                "Sid": "AllowScopedEC2InstanceActionsWithTags",
                "Effect": "Allow",
                "Resource": ["*"],
                #                    f"arn:{aws_partition}:ec2:{aws_region}:*:fleet/*",
                #                    f"arn:{aws_partition}:ec2:{aws_region}:*:instance/*",
                #                    f"arn:{aws_partition}:ec2:{aws_region}:*:volume/*",
                #                    f"arn:{aws_partition}:ec2:{aws_region}:*:network-interface/*",
                #                    f"arn:{aws_partition}:ec2:{aws_region}:*:launch-template/*",
                #                    f"arn:{aws_partition}:ec2:{aws_region}:*:spot-instances-request/*",
                #                    f"arn:{aws_partition}:ec2:{aws_region}:*:capacity-reservation/*",
                #                ],
                "Action": [
                    "ec2:RunInstances",
                    "ec2:CreateFleet",
                    "ec2:CreateLaunchTemplate",
                ],
                #                "Condition": {
                #                    "StringEquals": {
                #                        f"aws:RequestTag/kubernetes.io/cluster/{cluster_name}": "owned",
                #                        f"aws:RequestTag/eks:eks-cluster-name": "{cluster_name}",
                #                    },
                #                    "StringLike": {"aws:RequestTag/karpenter.sh/nodepool": "*"},
                #                },
            },
            {
                "Sid": "AllowScopedResourceCreationTagging",
                "Effect": "Allow",
                "Resource": [
                    f"arn:{aws_partition}:ec2:{aws_region}:*:fleet/*",
                    f"arn:{aws_partition}:ec2:{aws_region}:*:instance/*",
                    f"arn:{aws_partition}:ec2:{aws_region}:*:volume/*",
                    f"arn:{aws_partition}:ec2:{aws_region}:*:network-interface/*",
                    f"arn:{aws_partition}:ec2:{aws_region}:*:launch-template/*",
                    f"arn:{aws_partition}:ec2:{aws_region}:*:spot-instances-request/*",
                ],
                "Action": "ec2:CreateTags",
                #                "Condition": {
                #                    "StringEquals": {
                #                        f"aws:RequestTag/kubernetes.io/cluster/{cluster_name}": "owned",
                #                        f"aws:RequestTag/eks:eks-cluster-name": "{cluster_name}",
                #                        "ec2:CreateAction": [
                #                            "RunInstances",
                #                            "CreateFleet",
                #                            "CreateLaunchTemplate",
                #                        ],
                #                    },
                #                    "StringLike": {"aws:RequestTag/karpenter.sh/nodepool": "*"},
                #                },
            },
            {
                "Sid": "AllowScopedResourceTagging",
                "Effect": "Allow",
                "Resource": f"arn:{aws_partition}:ec2:{aws_region}:*:instance/*",
                "Action": "ec2:CreateTags",
                "Condition": {
                    "StringEquals": {
                        f"aws:ResourceTag/kubernetes.io/cluster/{cluster_name}": "owned"
                    },
                    "StringLike": {"aws:ResourceTag/karpenter.sh/nodepool": "*"},
                    "StringEqualsIfExists": {
                        "aws:RequestTag/eks:eks-cluster-name": "{cluster_name}"
                    },
                    "ForAllValues:StringEquals": {
                        "aws:TagKeys": [
                            "eks:eks-cluster-name",
                            "karpenter.sh/nodeclaim",
                            "Name",
                        ]
                    },
                },
            },
            {
                "Sid": "AllowScopedDeletion",
                "Effect": "Allow",
                "Resource": [
                    f"arn:{aws_partition}:ec2:{aws_region}:*:instance/*",
                    f"arn:{aws_partition}:ec2:{aws_region}:*:launch-template/*",
                ],
                "Action": ["ec2:TerminateInstances", "ec2:DeleteLaunchTemplate"],
                "Condition": {
                    "StringEquals": {
                        f"aws:ResourceTag/kubernetes.io/cluster/{cluster_name}": "owned"
                    },
                    "StringLike": {"aws:ResourceTag/karpenter.sh/nodepool": "*"},
                },
            },
            {
                "Sid": "AllowRegionalReadActions",
                "Effect": "Allow",
                "Resource": "*",
                "Action": [
                    "ec2:DescribeCapacityReservations",
                    "ec2:DescribeImages",
                    "ec2:DescribeInstances",
                    "ec2:DescribeInstanceTypeOfferings",
                    "ec2:DescribeInstanceTypes",
                    "ec2:DescribeLaunchTemplates",
                    "ec2:DescribeSecurityGroups",
                    "ec2:DescribeSpotPriceHistory",
                    "ec2:DescribeSubnets",
                ],
                "Condition": {"StringEquals": {"aws:RequestedRegion": f"{aws_region}"}},
            },
            {
                "Sid": "AllowSSMReadActions",
                "Effect": "Allow",
                "Resource": f"arn:{aws_partition}:ssm:{aws_region}::parameter/aws/service/*",
                "Action": "ssm:GetParameter",
            },
            {
                "Sid": "AllowPricingReadActions",
                "Effect": "Allow",
                "Resource": "*",
                "Action": "pricing:GetProducts",
            },
            {
                "Sid": "AllowInterruptionQueueActions",
                "Effect": "Allow",
                "Resource": karpenter_interruption_queue_arn,
                "Action": [
                    "sqs:DeleteMessage",
                    "sqs:GetQueueUrl",
                    "sqs:ReceiveMessage",
                ],
            },
            {
                "Sid": "AllowPassingInstanceRole",
                "Effect": "Allow",
                "Resource": karpenter_node_role_arn,
                "Action": "iam:PassRole",
                "Condition": {
                    "StringEquals": {
                        "iam:PassedToService": [
                            "ec2.amazonaws.com",
                            "ec2.amazonaws.com.cn",
                        ]
                    }
                },
            },
            {
                "Sid": "AllowScopedInstanceProfileCreationActions",
                "Effect": "Allow",
                "Resource": f"arn:{aws_partition}:iam::{aws_account_id}:instance-profile/*",
                "Action": ["iam:CreateInstanceProfile"],
                "Condition": {
                    "StringEquals": {
                        f"aws:RequestTag/kubernetes.io/cluster/{cluster_name}": "owned",
                        "aws:RequestTag/eks:eks-cluster-name": "{cluster_name}",
                        "aws:RequestTag/topology.kubernetes.io/region": "{aws_region}",
                    },
                    "StringLike": {
                        "aws:RequestTag/karpenter.k8s.aws/ec2nodeclass": "*"
                    },
                },
            },
            {
                "Sid": "AllowScopedInstanceProfileTagActions",
                "Effect": "Allow",
                "Resource": f"arn:{aws_partition}:iam::{aws_account_id}:instance-profile/*",
                "Action": ["iam:TagInstanceProfile"],
                "Condition": {
                    "StringEquals": {
                        f"aws:ResourceTag/kubernetes.io/cluster/{cluster_name}": "owned",
                        "aws:ResourceTag/topology.kubernetes.io/region": "{aws_region}",
                        f"aws:RequestTag/kubernetes.io/cluster/{cluster_name}": "owned",
                        "aws:RequestTag/eks:eks-cluster-name": "{cluster_name}",
                        "aws:RequestTag/topology.kubernetes.io/region": "{aws_region}",
                    },
                    "StringLike": {
                        "aws:ResourceTag/karpenter.k8s.aws/ec2nodeclass": "*",
                        "aws:RequestTag/karpenter.k8s.aws/ec2nodeclass": "*",
                    },
                },
            },
            {
                "Sid": "AllowScopedInstanceProfileActions",
                "Effect": "Allow",
                "Resource": f"arn:{aws_partition}:iam::{aws_account_id}:instance-profile/*",
                "Action": [
                    "iam:AddRoleToInstanceProfile",
                    "iam:RemoveRoleFromInstanceProfile",
                    "iam:DeleteInstanceProfile",
                ],
                "Condition": {
                    "StringEquals": {
                        f"aws:ResourceTag/kubernetes.io/cluster/{cluster_name}": "owned",
                        "aws:ResourceTag/topology.kubernetes.io/region": "{aws_region}",
                    },
                    "StringLike": {
                        "aws:ResourceTag/karpenter.k8s.aws/ec2nodeclass": "*"
                    },
                },
            },
            {
                "Sid": "AllowInstanceProfileReadActions",
                "Effect": "Allow",
                "Resource": f"arn:{aws_partition}:iam::{aws_account_id}:instance-profile/*",
                "Action": "iam:GetInstanceProfile",
            },
            {
                "Sid": "AllowUnscopedInstanceProfileListAction",
                "Effect": "Allow",
                "Resource": "*",
                "Action": "iam:ListInstanceProfiles",
            },
            {
                "Sid": "AllowAPIServerEndpointDiscovery",
                "Effect": "Allow",
                "Resource": f"arn:{aws_partition}:eks:{aws_region}:{aws_account_id}:cluster/{cluster_name}",
                "Action": "eks:DescribeCluster",
            },
        ],
    }
