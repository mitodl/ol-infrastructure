from functools import lru_cache

import boto3

eks_client = boto3.client("eks")

# A global toleration to allow operators to run on nodes that
# are tainted 'operations' if there are any in the cluster
#
# Taints repel work, so no workload will be scheduled on a
# node tainted as such UNLESS that workload has the
# toleration below.
operations_toleration = [
    {
        "key": "operations",
        "operator": "Equal",
        "value": "true",
        "effect": "NoSchedule",
    },
]

# A global node affinity to draw work towards nodes labeled
# with ol.mit.edu/worker-class=core'
#
# Affinities attract work, so workloads configured with
# this work MUST be scheduled on nodes that have this label.
#
# Other workloads may exist on these labeled nodes, as well.
core_node_affinity = {
    "nodeAffinity": {
        "requiredDuringSchedulingIgnoredDuringExecution": {
            "labelSelector": [
                {
                    "matchExpressions": [
                        {
                            "key": "ol.mit.edu/worker-class",
                            "operator": "In",
                            "values": [
                                "core",
                            ],
                        },
                    ],
                },
            ]
        },
    },
}

# Ref: https://aws-quickstart.github.io/cdk-eks-blueprints/addons/coredns/
# Ref: https://repost.aws/knowledge-center/eks-managed-add-on
# aws eks describe-addon-configuration --addon-name coredns --addon-version v1.11.3-eksbuild.2 --query configurationSchema --output text | jq  # noqa: E501
coredns_configuration_values = {
    "resources": {
        "requests": {
            "memory": "100Mi",
            "cpu": "100m",
        },
        "limits": {
            "memory": "100Mi",
            "cpu": "100m",
        },
    },
    "replicaCount": 3,
    "affinity": {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "kubernetes.io/os",
                                "operator": "In",
                                "values": ["linux"],
                            },
                            {
                                "key": "kubernetes.io/arch",
                                "operator": "In",
                                "values": ["amd64", "arm64"],
                            },
                            {
                                "key": "ol.mit.edu/worker-class",
                                "operator": "In",
                                "values": ["core"],
                            },
                        ]
                    }
                ],
            }
        },
        "podAntiAffinity": {
            "preferredDuringSchedulingIgnoredDuringExecution": [
                {
                    "podAffinityTerm": {
                        "labelSelector": {
                            "matchExpressions": [
                                {
                                    "key": "k8s-app",
                                    "operator": "In",
                                    "values": ["kube-dns"],
                                }
                            ]
                        },
                        "topologyKey": "kubernetes.io/hostname",
                    },
                    "weight": 100,
                }
            ]
        },
    },
    "tolerations": [
        {"key": "CriticalAddonsOnly", "operator": "Exists"},
        {"key": "node-role.kubernetes.io/master", "operator": "NoSchedule"},
        {"key": "operations", "operator": "Exists"},
    ],
}


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
