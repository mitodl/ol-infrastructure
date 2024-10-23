from functools import lru_cache

import boto3

eks_client = boto3.client("eks")


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
