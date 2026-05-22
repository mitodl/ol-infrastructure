"""Install the marimo Operator on the data EKS cluster.

The marimo operator manages MarimoNotebook CRDs (``marimo.io/v1alpha1``).
For each ``MarimoNotebook`` resource it creates a pod, a persistent volume
claim (when storage is configured), and a per-notebook ``ClusterIP`` Service
named after the notebook.  There is no central gateway — individual notebooks
are accessed through their own Service.

The operator is installed from the upstream ``deploy/install.yaml`` manifest
so that the version is pinned precisely and Renovate can track releases via
the ``github-releases`` datasource in ``bridge/lib/versions.py``.

Refs:
  https://docs.marimo.io/guides/deploying/deploying_kubernetes/
  https://github.com/marimo-team/marimo-operator
"""

import urllib.request
from typing import Any

import pulumi_kubernetes as kubernetes
import yaml as pyyaml
from pulumi import ResourceOptions, StackReference

from bridge.lib.versions import MARIMO_OPERATOR_VERSION
from ol_infrastructure.lib.aws.eks_helper import check_cluster_namespace

MARIMO_OPERATOR_MANIFEST_URL = (
    "https://raw.githubusercontent.com/marimo-team/marimo-operator/"
    f"{MARIMO_OPERATOR_VERSION}/deploy/install.yaml"
)
MARIMO_OPERATOR_NAMESPACE = "marimo-operator-system"


def _fetch_operator_manifests() -> list[dict[str, Any]]:
    """Fetch and parse the marimo operator install YAML.

    Returns:
        List of Kubernetes resource dicts from the install manifest.
    """
    with urllib.request.urlopen(MARIMO_OPERATOR_MANIFEST_URL) as response:  # noqa: S310
        content = response.read().decode("utf-8")
    return [doc for doc in pyyaml.safe_load_all(content) if doc is not None]


def setup_marimo_operator(
    cluster_name: str,
    cluster_stack: StackReference,
    k8s_provider: kubernetes.Provider,
) -> kubernetes.yaml.v2.ConfigGroup:
    """Install the marimo Operator into the marimo-operator-system namespace.

    The operator is installed from the upstream ``deploy/install.yaml``
    manifest, which bundles the ``MarimoNotebook`` CRD, RBAC resources, and
    the operator Deployment.  It watches all namespaces and reconciles
    ``MarimoNotebook`` resources by creating per-notebook Deployments and
    ClusterIP Services.

    Args:
        cluster_name: Name of the EKS cluster (used for Pulumi resource names).
        cluster_stack: StackReference to the EKS infrastructure stack, used to
            validate that the marimo-operator-system namespace exists.
        k8s_provider: Pulumi Kubernetes provider for this cluster.

    Returns:
        The ConfigGroup resource representing the operator install.
    """
    cluster_stack.require_output("namespaces").apply(
        lambda ns: check_cluster_namespace(MARIMO_OPERATOR_NAMESPACE, ns)
    )

    return kubernetes.yaml.v2.ConfigGroup(
        f"{cluster_name}-marimo-operator",
        objs=_fetch_operator_manifests(),
        opts=ResourceOptions(
            provider=k8s_provider,
            delete_before_replace=True,
        ),
    )
