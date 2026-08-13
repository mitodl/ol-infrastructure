import json
import re
from functools import lru_cache, partial
from typing import Any

import boto3
import pulumi
from botocore.exceptions import ClientError
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


EKS_ADDON_VERSION_RE = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:-eksbuild\.(?P<build>\d+))?$"
)


def eks_addon_version_sort_key(addon_version: str) -> tuple[int, int, int, int]:
    """Order EKS addon versions numerically rather than lexicographically.

    Addon versions look like ``v1.63.0-eksbuild.1``. Sorting those as plain
    strings is wrong at every digit boundary -- ``v1.9.0-eksbuild.1`` sorts
    *above* ``v1.63.0-eksbuild.1`` because "9" > "6" -- so a lexicographic
    "newest" pick silently selects a years-old addon.

    Args:
        addon_version: An addon version string, e.g. ``v1.63.0-eksbuild.1``.

    Returns:
        A tuple ordering versions numerically. Unparseable versions sort below
        every well-formed one instead of raising, so an unexpected format from
        AWS degrades the ordering rather than breaking the deploy.
    """
    match = EKS_ADDON_VERSION_RE.match(addon_version)
    if match is None:
        return (-1, -1, -1, -1)
    return (
        int(match["major"]),
        int(match["minor"]),
        int(match["patch"]),
        int(match["build"] or 0),
    )


@lru_cache
def get_eks_addon_version(
    addon_name: str,
    cluster_version: str | None = None,
    pinned_version: str | None = None,
) -> str:
    """Resolve the addon version to install, honouring an explicit pin.

    Without ``pinned_version`` this returns the newest version AWS offers for
    ``cluster_version``, which means the addon silently tracks latest and can take
    a major-version jump with no PR, no review, and no staged rollout. Pass
    ``pinned_version`` (from ``bridge.lib.versions``, where Renovate manages it) so
    addon upgrades go through code review like every other dependency.

    Args:
        addon_name: EKS addon name, e.g. ``aws-efs-csi-driver``.
        cluster_version: Kubernetes version to resolve addon versions against.
            Defaults to ``get_cluster_version()``, which is the newest
            standard-support EKS Kubernetes version -- not the version of any
            particular cluster. Pass an explicit value to resolve against a
            cluster that is not on the newest release.
        pinned_version: Exact addon version to install. Must be offered by AWS for
            ``cluster_version``.

    Returns:
        The addon version to install.

    Raises:
        ValueError: If ``pinned_version`` is not available for this cluster
            version, rather than silently falling back to latest.
    """
    if cluster_version is None:
        cluster_version = get_cluster_version()
    version_info = eks_client.describe_addon_versions(
        kubernetesVersion=cluster_version,
        addonName=addon_name,
    )["addons"][0]
    versions = [version["addonVersion"] for version in version_info["addonVersions"]]
    if pinned_version is not None:
        if pinned_version not in versions:
            available = sorted(versions, key=eks_addon_version_sort_key, reverse=True)
            msg = (
                f"Pinned version {pinned_version} of EKS addon {addon_name} is not "
                f"available for Kubernetes {cluster_version}. Available versions: "
                f"{', '.join(available)}"
            )
            raise ValueError(msg)
        return pinned_version
    return max(versions, key=eks_addon_version_sort_key)


@lru_cache
def get_k8s_provider(
    kubeconfig: pulumi.Output[Any] | str,
    provider_name: str | None,
):
    return Provider(
        provider_name or "k8s-provider",
        kubeconfig=kubeconfig,
    )


def set_k8s_provider(
    kubeconfig: pulumi.Output[Any] | str,
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
    kubeconfig: pulumi.Output[Any] | str | dict[str, object],
    provider_name: str | None = None,
):
    # lru_cache requires hashable arguments; serialize dict kubeconfigs to JSON
    if isinstance(kubeconfig, dict):
        kubeconfig = json.dumps(kubeconfig)
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


def access_entry_opts(
    cluster_name: str,
    principal_arn: str,
) -> tuple[pulumi.ResourceOptions, str]:
    """Look up and conditionally import an existing EKS access entry.

    This function checks if an access entry already exists for the given principal
    in the cluster. If it exists, it will be imported into Pulumi state.

    :param cluster_name: The name of the EKS cluster
    :type cluster_name: str

    :param principal_arn: The ARN of the IAM principal (role/user)
    :type principal_arn: str

    :returns: A Pulumi ResourceOptions object for importing the access entry
              and the composite import ID or empty string if not found
    :rtype: Tuple[pulumi.ResourceOptions, str]
    """
    resource_id = ""

    try:
        eks_client.describe_access_entry(
            clusterName=cluster_name,
            principalArn=principal_arn,
        )
        # Access entry exists, set up for import
        # Import ID format: cluster_name:principal_arn
        resource_id = f"{cluster_name}:{principal_arn}"
        opts = pulumi.ResourceOptions(
            import_=resource_id,
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
        # Access entry doesn't exist, create new one
        opts = pulumi.ResourceOptions()

    return opts, resource_id
