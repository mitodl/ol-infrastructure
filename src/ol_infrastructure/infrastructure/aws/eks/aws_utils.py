# ruff: noqa: E501, PLR0913
"""Pulumi components for configuring AWS integrations into EKS."""

import json

import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
from pulumi import ResourceOptions

from ol_infrastructure.components.aws.eks import OLEKSTrustRole, OLEKSTrustRoleConfig
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION


def setup_aws_integrations(
    aws_account,
    cluster_name,
    cluster,
    aws_config,
    k8s_global_labels,
    k8s_provider,
    operations_tolerations,
    target_vpc,
    node_groups,
    versions,
):
    """
    Set up AWS integrations for EKS.

    This includes the AWS Load Balancer Controller and the AWS Node Termination Handler.
    """
    ############################################################
    # Install and configure AWS Load Balancer Controller
    ############################################################
    # Ref: https://kubernetes-sigs.github.io/aws-load-balancer-controller/v2.13.4/deploy/
    # Ref: https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/v2.13.4/docs/install/iam_policy.json

    aws_load_balancer_controller_policy_document = {
        "Version": IAM_POLICY_VERSION,
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["iam:CreateServiceLinkedRole"],
                "Resource": "*",
                "Condition": {
                    "StringEquals": {
                        "iam:AWSServiceName": "elasticloadbalancing.amazonaws.com"
                    }
                },
            },
            {
                "Effect": "Allow",
                "Action": [
                    "ec2:DescribeAccountAttributes",
                    "ec2:DescribeAddresses",
                    "ec2:DescribeAvailabilityZones",
                    "ec2:DescribeInternetGateways",
                    "ec2:DescribeVpcs",
                    "ec2:DescribeVpcPeeringConnections",
                    "ec2:DescribeSubnets",
                    "ec2:DescribeSecurityGroups",
                    "ec2:DescribeInstances",
                    "ec2:DescribeNetworkInterfaces",
                    "ec2:DescribeTags",
                    "ec2:GetCoipPoolUsage",
                    "ec2:DescribeCoipPools",
                    "ec2:GetSecurityGroupsForVpc",
                    "ec2:DescribeIpamPools",
                    "ec2:DescribeRouteTables",
                    "elasticloadbalancing:DescribeLoadBalancers",
                    "elasticloadbalancing:DescribeLoadBalancerAttributes",
                    "elasticloadbalancing:DescribeListeners",
                    "elasticloadbalancing:DescribeListenerCertificates",
                    "elasticloadbalancing:DescribeSSLPolicies",
                    "elasticloadbalancing:DescribeRules",
                    "elasticloadbalancing:DescribeTargetGroups",
                    "elasticloadbalancing:DescribeTargetGroupAttributes",
                    "elasticloadbalancing:DescribeTargetHealth",
                    "elasticloadbalancing:DescribeTags",
                    "elasticloadbalancing:DescribeTrustStores",
                    "elasticloadbalancing:DescribeListenerAttributes",
                    "elasticloadbalancing:DescribeCapacityReservation",
                ],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "cognito-idp:DescribeUserPoolClient",
                    "acm:ListCertificates",
                    "acm:DescribeCertificate",
                    "iam:ListServerCertificates",
                    "iam:GetServerCertificate",
                    "waf-regional:GetWebACL",
                    "waf-regional:GetWebACLForResource",
                    "waf-regional:AssociateWebACL",
                    "waf-regional:DisassociateWebACL",
                    "wafv2:GetWebACL",
                    "wafv2:GetWebACLForResource",
                    "wafv2:AssociateWebACL",
                    "wafv2:DisassociateWebACL",
                    "shield:GetSubscriptionState",
                    "shield:DescribeProtection",
                    "shield:CreateProtection",
                    "shield:DeleteProtection",
                ],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "ec2:AuthorizeSecurityGroupIngress",
                    "ec2:RevokeSecurityGroupIngress",
                ],
                "Resource": "*",
            },
            {"Effect": "Allow", "Action": ["ec2:CreateSecurityGroup"], "Resource": "*"},
            {
                "Effect": "Allow",
                "Action": ["ec2:CreateTags"],
                "Resource": "arn:aws:ec2:*:*:security-group/*",
                "Condition": {
                    "StringEquals": {"ec2:CreateAction": "CreateSecurityGroup"},
                    "Null": {"aws:RequestTag/elbv2.k8s.aws/cluster": "false"},
                },
            },
            {
                "Effect": "Allow",
                "Action": ["ec2:CreateTags", "ec2:DeleteTags"],
                "Resource": "arn:aws:ec2:*:*:security-group/*",
                "Condition": {
                    "Null": {
                        "aws:RequestTag/elbv2.k8s.aws/cluster": "true",
                        "aws:ResourceTag/elbv2.k8s.aws/cluster": "false",
                    }
                },
            },
            {
                "Effect": "Allow",
                "Action": [
                    "ec2:AuthorizeSecurityGroupIngress",
                    "ec2:RevokeSecurityGroupIngress",
                    "ec2:DeleteSecurityGroup",
                ],
                "Resource": "*",
                "Condition": {
                    "Null": {"aws:ResourceTag/elbv2.k8s.aws/cluster": "false"}
                },
            },
            {
                "Effect": "Allow",
                "Action": [
                    "elasticloadbalancing:CreateLoadBalancer",
                    "elasticloadbalancing:CreateTargetGroup",
                ],
                "Resource": "*",
                "Condition": {
                    "Null": {"aws:RequestTag/elbv2.k8s.aws/cluster": "false"}
                },
            },
            {
                "Effect": "Allow",
                "Action": [
                    "elasticloadbalancing:CreateListener",
                    "elasticloadbalancing:DeleteListener",
                    "elasticloadbalancing:CreateRule",
                    "elasticloadbalancing:DeleteRule",
                ],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "elasticloadbalancing:AddTags",
                    "elasticloadbalancing:RemoveTags",
                ],
                "Resource": [
                    "arn:aws:elasticloadbalancing:*:*:targetgroup/*/*",
                    "arn:aws:elasticloadbalancing:*:*:loadbalancer/net/*/*",
                    "arn:aws:elasticloadbalancing:*:*:loadbalancer/app/*/*",
                ],
                "Condition": {
                    "Null": {
                        "aws:RequestTag/elbv2.k8s.aws/cluster": "true",
                        "aws:ResourceTag/elbv2.k8s.aws/cluster": "false",
                    }
                },
            },
            {
                "Effect": "Allow",
                "Action": [
                    "elasticloadbalancing:AddTags",
                    "elasticloadbalancing:RemoveTags",
                ],
                "Resource": [
                    "arn:aws:elasticloadbalancing:*:*:listener/net/*/*/*",
                    "arn:aws:elasticloadbalancing:*:*:listener/app/*/*/*",
                    "arn:aws:elasticloadbalancing:*:*:listener-rule/net/*/*/*",
                    "arn:aws:elasticloadbalancing:*:*:listener-rule/app/*/*/*",
                ],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "elasticloadbalancing:ModifyLoadBalancerAttributes",
                    "elasticloadbalancing:SetIpAddressType",
                    "elasticloadbalancing:SetSecurityGroups",
                    "elasticloadbalancing:SetSubnets",
                    "elasticloadbalancing:DeleteLoadBalancer",
                    "elasticloadbalancing:ModifyTargetGroup",
                    "elasticloadbalancing:ModifyTargetGroupAttributes",
                    "elasticloadbalancing:DeleteTargetGroup",
                    "elasticloadbalancing:ModifyListenerAttributes",
                    "elasticloadbalancing:ModifyCapacityReservation",
                    "elasticloadbalancing:ModifyIpPools",
                ],
                "Resource": "*",
                "Condition": {
                    "Null": {"aws:ResourceTag/elbv2.k8s.aws/cluster": "false"}
                },
            },
            {
                "Effect": "Allow",
                "Action": ["elasticloadbalancing:AddTags"],
                "Resource": [
                    "arn:aws:elasticloadbalancing:*:*:targetgroup/*/*",
                    "arn:aws:elasticloadbalancing:*:*:loadbalancer/net/*/*",
                    "arn:aws:elasticloadbalancing:*:*:loadbalancer/app/*/*",
                ],
                "Condition": {
                    "StringEquals": {
                        "elasticloadbalancing:CreateAction": [
                            "CreateTargetGroup",
                            "CreateLoadBalancer",
                        ]
                    },
                    "Null": {"aws:RequestTag/elbv2.k8s.aws/cluster": "false"},
                },
            },
            {
                "Effect": "Allow",
                "Action": [
                    "elasticloadbalancing:RegisterTargets",
                    "elasticloadbalancing:DeregisterTargets",
                ],
                "Resource": "arn:aws:elasticloadbalancing:*:*:targetgroup/*/*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "elasticloadbalancing:SetWebAcl",
                    "elasticloadbalancing:ModifyListener",
                    "elasticloadbalancing:AddListenerCertificates",
                    "elasticloadbalancing:RemoveListenerCertificates",
                    "elasticloadbalancing:ModifyRule",
                    "elasticloadbalancing:SetRulePriorities",
                ],
                "Resource": "*",
            },
        ],
    }

    aws_lb_controller_service_account_name = "aws-load-balancer-controller"

    aws_load_balancer_controller_role_config = OLEKSTrustRoleConfig(
        account_id=aws_account.account_id,
        cluster_name=cluster_name,
        cluster_identities=cluster.eks_cluster.identities,
        description="Trust role for allowing the AWS Load Balancer Controller to manage ALBs/NLBs.",
        policy_operator="StringEquals",
        role_name=aws_lb_controller_service_account_name,
        service_account_identifier="system:serviceaccount:kube-system:aws-load-balancer-controller",
        tags=aws_config.tags,
    )
    aws_load_balancer_controller_role = OLEKSTrustRole(
        f"{cluster_name}-aws-load-balancer-controller-trust-role",
        role_config=aws_load_balancer_controller_role_config,
        opts=ResourceOptions(parent=cluster, depends_on=cluster),
    )

    kubernetes.core.v1.ServiceAccount(
        "aws-lb-controller-service-account",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=aws_lb_controller_service_account_name,
            namespace="kube-system",
            labels=k8s_global_labels,
            annotations={
                "eks.amazonaws.com/role-arn": aws_load_balancer_controller_role.role.arn
            },
        ),
        automount_service_account_token=True,
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=cluster,
            depends_on=[
                cluster,
            ],
        ),
    )

    aws_load_balancer_controller_policy = aws.iam.Policy(
        f"{cluster_name}-aws-load-balancer-controller-policy",
        name=f"{cluster_name}-aws-load-balancer-controller-policy",
        path=f"/ol-infrastructure/eks/{cluster_name}/",
        policy=json.dumps(
            aws_load_balancer_controller_policy_document,
        ),
        opts=ResourceOptions(
            parent=aws_load_balancer_controller_role, depends_on=cluster
        ),
    )
    aws.iam.RolePolicyAttachment(
        f"{cluster_name}-aws-load-balancer-controller-attachment",
        policy_arn=aws_load_balancer_controller_policy.arn,
        role=aws_load_balancer_controller_role.role.id,
        opts=ResourceOptions(parent=aws_load_balancer_controller_role),
    )

    kubernetes.helm.v3.Release(
        f"{cluster_name}-aws-load-balancer-controller-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="aws-load-balancer-controller",
            chart="aws-load-balancer-controller",
            version=versions["AWS_LOAD_BALANCER_CONTROLLER_CHART"],
            namespace="kube-system",
            cleanup_on_fail=True,
            timeout=600,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://aws.github.io/eks-charts",
            ),
            values={
                "clusterName": cluster_name,
                "enableCertManager": True,
                "serviceAccount": {
                    "create": False,
                    "name": aws_lb_controller_service_account_name,
                    "annotations": {
                        "eks.amazonaws.com/role-arn": aws_load_balancer_controller_role.role.arn,
                    },
                },
                "vpcId": target_vpc["id"],
                "region": aws.get_region().name,
                "podLabels": k8s_global_labels,
                "tolerations": operations_tolerations,
                "resources": {
                    "requests": {
                        "cpu": "100m",
                        "memory": "128Mi",
                    },
                    "limits": {
                        "cpu": "200m",
                        "memory": "256Mi",
                    },
                },
            },
            skip_await=False,
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=cluster,
            depends_on=[
                cluster,
                node_groups[0],
                aws_load_balancer_controller_role,
                aws_load_balancer_controller_policy,
            ],
            delete_before_replace=True,
        ),
    )

    ############################################################
    # Install AWS Node Termination Handler
    ############################################################
    # Ref: https://github.com/aws/aws-node-termination-handler/blob/v1.25.2/config/helm/aws-node-termination-handler/values.yaml
    kubernetes.helm.v3.Release(
        f"{cluster_name}-aws-node-termination-handler-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="aws-node-termination-handler",
            chart="oci://public.ecr.aws/aws-ec2/helm/aws-node-termination-handler",
            version=versions["AWS_NODE_TERMINATION_HANDLER_CHART"],
            namespace="kube-system",
            cleanup_on_fail=True,
            skip_await=True,
            values={
                "podLabels": k8s_global_labels,
                "serviceAccount": {
                    "labels": k8s_global_labels,
                },
                "targetNodeOs": "linux",
                "enableProbesServer": True,
                "enableSqsTerminationDraining": False,
                "enableSpotInterruptionDraining": True,
                "enableASGLifecycleDraining": True,
                "enableScheduledEventDraining": True,
                "enableRebalanceMonitoring": True,
                "enableRebalanceDraining": True,
                "emitKubernetesEvents": True,
                "podTerminationGracePeriod": 30,
                "resources": {
                    "requests": {
                        "cpu": "50m",
                        "memory": "24Mi",
                    },
                    "limits": {
                        "cpu": "50m",
                        "memory": "24Mi",
                    },
                },
            },
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=cluster,
            delete_before_replace=True,
        ),
    )
