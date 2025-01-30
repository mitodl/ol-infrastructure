from functools import lru_cache, partial
from typing import Optional

import boto3
import pulumi
from pulumi_aws import ec2
from pulumi_kubernetes import Provider

eks_client = boto3.client("eks")

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
def eks_versions(reference_addon: str = "aws-ebs-csi-driver") -> list[str]:
    """Return a list of valid, supported EKS versions.

    There is no AWS API call to simply return the currently supported
    list of K8S resleases in EKS. So we do a hack here where we query a
    'universal add-on', in this case the EBS CSI driver by default,
    for the versions of K8S that it is compabitble. The reasoning being
    that it seems unlikely that AWS will stop supporting this addon
    any time soon.

    :param reference_addon: Addon to use when performing the lookup.
    :type reference_addon: str
    """
    addons_response = eks_client.describe_addon_versions(addonName=reference_addon)
    compatabilities_list = addons_response["addons"][0]["addonVersions"][0][
        "compatibilities"
    ]
    return [compat_def["clusterVersion"] for compat_def in compatabilities_list]


@lru_cache
def get_k8s_provider(
    kubeconfig: str,
    provider_name: Optional[str],
):
    return Provider(
        provider_name or "k8s-provider",
        kubeconfig=kubeconfig,
    )


def set_k8s_provider(
    kubeconfig: str,
    provider_name: Optional[str],
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
    provider_name: Optional[str] = None,
):
    pulumi.runtime.register_stack_transformation(
        partial(
            set_k8s_provider,
            kubeconfig,
            provider_name,
        )
    )
