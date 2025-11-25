# ruff: noqa: E501, PLR0913
import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
from pulumi import Output, ResourceOptions, StackReference, export

from bridge.lib.magic_numbers import DEFAULT_HTTPS_PORT, DEFAULT_KEDA_PORT
from bridge.lib.versions import KEDA_CHART_VERSION
from ol_infrastructure.lib.aws.eks_helper import default_psg_egress_args
from ol_infrastructure.lib.ol_types import AWSBase


def setup_keda(
    cluster_name: str,
    cluster_stack: StackReference,
    target_vpc: Output[dict],
    aws_config: AWSBase,
    k8s_provider: kubernetes.Provider,
    k8s_global_labels: dict[str, str],
):
    """
    Set up KEDA (Kubernetes Event Driven Autoscaling) resources including security group,
    Helm chart installation, and security group policy.

    Args:
        cluster_name: The name of the EKS cluster.
        cluster_stack: A StackReference to the EKS cluster stack.
        target_vpc: The target VPC output containing vpc information.
        aws_config: The AWS configuration object containing tags.
        k8s_provider: The Pulumi Kubernetes provider instance.
        k8s_global_labels: A dictionary of global labels to apply to Kubernetes resources.
    """
    keda_security_group = aws.ec2.SecurityGroup(
        f"{cluster_name}-keda-security-group",
        description="Security group for KEDA operator",
        vpc_id=target_vpc["id"],
        ingress=[
            aws.ec2.SecurityGroupIngressArgs(
                self=True,
                from_port=DEFAULT_KEDA_PORT,
                to_port=DEFAULT_KEDA_PORT,
                protocol="tcp",
            ),
            aws.ec2.SecurityGroupIngressArgs(
                self=True,
                from_port=DEFAULT_HTTPS_PORT,
                to_port=DEFAULT_HTTPS_PORT,
                protocol="tcp",
            ),
            aws.ec2.SecurityGroupIngressArgs(
                self=True,
                from_port=8080,
                to_port=8080,
                protocol="tcp",
            ),
            aws.ec2.SecurityGroupIngressArgs(
                from_port=6443,
                to_port=6443,
                security_groups=[
                    cluster_stack.require_output("cluster_security_group_id")
                ],
                protocol="tcp",
            ),
        ],
        egress=default_psg_egress_args,
        tags={
            **aws_config.tags,
            "Name": f"{cluster_name}-keda-security-group",
        },
    )
    export("cluster_keda_security_group_id", keda_security_group.id)

    keda_release = kubernetes.helm.v3.Release(
        f"{cluster_name}-keda-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="keda",
            chart="keda",
            version=KEDA_CHART_VERSION,
            namespace="operations",
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://kedacore.github.io/charts"
            ),
            cleanup_on_fail=True,
            skip_await=True,
            values={
                "podLabels": {
                    "keda": {
                        "ol.mit.edu/pod-security-group": keda_security_group.id,
                    },
                    "metricsAdapter": {
                        "ol.mit.edu/pod-security-group": keda_security_group.id,
                    },
                    "webhooks": {
                        "ol.mit.edu/pod-security-group": keda_security_group.id,
                    },
                },
                "resources": {
                    "operator": {
                        "requests": {
                            "cpu": "10m",
                            "memory": "400Mi",
                        },
                        "limits": {
                            "memory": "400Mi",
                        },
                    },
                    "metricServer": {
                        "requests": {
                            "cpu": "10m",
                            "memory": "100Mi",
                        },
                        "limits": {
                            "memory": "100Mi",
                        },
                    },
                    "webhooks": {
                        "requests": {
                            "cpu": "10m",
                            "memory": "40Mi",
                        },
                        "limits": {
                            "memory": "40Mi",
                        },
                    },
                },
            },
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=k8s_provider,
            delete_before_replace=True,
        ),
    )

    kubernetes.apiextensions.CustomResource(
        f"{cluster_name}-keda-helm-release",
        api_version="vpcresources.k8s.aws/v1beta1",
        kind="SecurityGroupPolicy",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="keda-operator",
            namespace="operations",
            labels=k8s_global_labels,
        ),
        spec={
            "podSelector": {
                "matchLabels": {
                    "ol.mit.edu/pod-security-group": keda_security_group.id,
                }
            },
            "securityGroups": {
                "groupIds": [keda_security_group.id],
            },
        },
        opts=ResourceOptions(depends_on=keda_release, provider=k8s_provider),
    )
