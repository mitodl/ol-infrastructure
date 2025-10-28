from functools import lru_cache, partial

import boto3
import pulumi
from packaging.version import Version
from pulumi_aws import ec2
from pulumi_kubernetes import Provider

from ol_infrastructure.lib.aws.aws_helper import AWS_ACCOUNT_ID

eks_client = boto3.client("eks")
ECR_DOCKERHUB_REGISTRY = f"{AWS_ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/dockerhub"

# Like our ec2 practices, allow pods to egress anywhere they want
default_psg_egress_args = [
    ec2.SecurityGroupEgressArgs(
        protocol="-1",
        from_port=0,
        to_port=0,
        cidr_blocks=["0.0.0.0/0"],
        ipv6_cidr_blocks=["::/0"],
    )
]


def get_default_psg_ingress_args(
    k8s_pod_subnet_cidrs: list[str],
) -> list[ec2.SecurityGroupIngressArgs]:
    return [
        ec2.SecurityGroupIngressArgs(
            protocol="-1",
            from_port=0,
            to_port=0,
            cidr_blocks=k8s_pod_subnet_cidrs,
            description="Allow ingress to the pod from anywhere in the k8s cluster.",
        ),
    ]


def check_cluster_namespace(namespace: str, namespaces: list[str]):
    """Verify that a namespace is available in an EKS cluster.

    :param namespace: The name of the namespace to verify.
    :type namespace: str

    :param namespaces: list of namespaces available in the cluster
    :type cluster_stakc: list[str]

    """
    if namespace not in namespaces:
        msg = f"namespace: {namespace} not in available namespaces: {namespaces}"
        raise ValueError(msg)


@lru_cache
def get_cluster_version(*, use_default: bool = True) -> str:
    """Get the current version of the EKS cluster."""
    if use_default:
        cluster_versions = eks_client.describe_cluster_versions(
            defaultOnly=use_default, clusterType="eks"
        )
    else:
        cluster_versions = eks_client.describe_cluster_versions(
            clusterType="eks", versionStatus="STANDARD_SUPPORT"
        )
    versions_list = sorted(
        [version["clusterVersion"] for version in cluster_versions["clusterVersions"]],
        key=Version,
        reverse=True,
    )
    return versions_list[0]


@lru_cache
def get_eks_addon_version(addon_name: str, cluster_version: str | None = None) -> str:
    if cluster_version is None:
        cluster_version = get_cluster_version()
    version_info = eks_client.describe_addon_versions(
        kubernetesVersion=cluster_version,
        addonName=addon_name,
    )["addons"][0]
    versions = [version["addonVersion"] for version in version_info["addonVersions"]]
    return sorted(versions, reverse=True)[0]


@lru_cache
def get_k8s_provider(
    kubeconfig: str,
    provider_name: str | None,
):
    return Provider(
        provider_name or "k8s-provider",
        kubeconfig=kubeconfig,
    )


def set_k8s_provider(
    kubeconfig: str,
    provider_name: str | None,
    resource_args: pulumi.ResourceTransformationArgs,
) -> pulumi.ResourceTransformationResult:
    if resource_args.type_.split(":")[0] == "kubernetes":
        resource_args.opts.provider = get_k8s_provider(
            kubeconfig,
            provider_name,
        )
    return pulumi.ResourceTransformationResult(
        props=resource_args.props,
        opts=resource_args.opts,
    )


def setup_k8s_provider(
    kubeconfig: str,
    provider_name: str | None = None,
):
    pulumi.runtime.register_stack_transformation(
        partial(
            set_k8s_provider,
            kubeconfig,
            provider_name,
        )
    )


def cached_image_uri(
    image_repo: str, aws_account_id: str | int = "610119931565"
) -> str:
    if len(image_repo.split("/")) < 2:  # noqa: PLR2004
        image_repo = f"library/{image_repo}"
    return f"{aws_account_id}.dkr.ecr.us-east-1.amazonaws.com/dockerhub/{image_repo}"


def ecr_image_uri(image_repo: str, aws_account_id: str | int = "610119931565") -> str:
    return f"{aws_account_id}.dkr.ecr.us-east-1.amazonaws.com/{image_repo}"
