"""Install the Altinity ClickHouse Operator on the data EKS cluster.

The Altinity Operator manages ClickHouseInstallation and
ClickHouseKeeperInstallation CRDs. It is installed via the upstream raw YAML
manifest so that the version can be pinned precisely and renovate can track
releases via the github-releases datasource.
"""

import urllib.request
from typing import Any

import pulumi_kubernetes as kubernetes
import yaml as pyyaml
from pulumi import Config, ResourceOptions, StackReference

from bridge.lib.versions import CLICKHOUSE_OPERATOR_VERSION
from ol_infrastructure.lib.aws.eks_helper import check_cluster_namespace

CLICKHOUSE_OPERATOR_MANIFEST_URL = (
    "https://raw.githubusercontent.com/Altinity/clickhouse-operator/"
    f"{CLICKHOUSE_OPERATOR_VERSION}/deploy/operator/clickhouse-operator-install-bundle.yaml"
)
CLICKHOUSE_OPERATOR_NAMESPACE = "clickhouse-operator"
# Where the install bundle actually puts the operator Deployment and its
# metrics Service. The bundle hardcodes kube-system; nothing is deployed into
# CLICKHOUSE_OPERATOR_NAMESPACE, which is only checked for existence.
CLICKHOUSE_OPERATOR_BUNDLE_NAMESPACE = "kube-system"


def _fetch_operator_manifests() -> list[dict[str, Any]]:
    """Fetch and parse the ClickHouse operator install-bundle YAML.

    The bundle uses YAML 1.1 features (anchors, aliases, ``!!merge`` merge keys)
    that Go's yaml library rejects. Python's PyYAML resolves all anchors and
    merge keys into plain dicts, which the Pulumi Kubernetes provider can then
    accept via ``objs=`` without any YAML parsing on the Go side.

    Returns:
        List of Kubernetes resource dicts from the install-bundle manifest.
    """
    with urllib.request.urlopen(CLICKHOUSE_OPERATOR_MANIFEST_URL) as response:  # noqa: S310
        content = response.read().decode("utf-8")
    return [doc for doc in pyyaml.safe_load_all(content) if doc is not None]


def setup_clickhouse_operator(
    cluster_name: str,
    cluster_stack: StackReference,
    k8s_provider: kubernetes.Provider,
) -> kubernetes.yaml.v2.ConfigGroup | None:
    """Install the Altinity ClickHouse Operator into the clickhouse-operator namespace.

    The operator is installed from the upstream install-bundle manifest, which
    includes CRDs (ClickHouseInstallation, ClickHouseKeeperInstallation, etc.)
    and the operator Deployment. It watches all namespaces by default.

    Skipped when ``clickhouse:enable_operator`` is not set to true in config.

    Args:
        cluster_name: Name of the EKS cluster (used for Pulumi resource names).
        cluster_stack: StackReference to the EKS infrastructure stack, used to
            validate that the clickhouse-operator namespace exists.
        k8s_provider: Pulumi Kubernetes provider for this cluster.

    Returns:
        The ConfigGroup resource representing the operator install, or None if
        the operator is not enabled for this stack.
    """
    clickhouse_config = Config("clickhouse")
    if not clickhouse_config.get_bool("enable_operator"):
        return None

    cluster_stack.require_output("namespaces").apply(
        lambda ns: check_cluster_namespace(CLICKHOUSE_OPERATOR_NAMESPACE, ns)
    )

    operator = kubernetes.yaml.v2.ConfigGroup(
        f"{cluster_name}-clickhouse-operator",
        objs=_fetch_operator_manifests(),
        opts=ResourceOptions(
            provider=k8s_provider,
            delete_before_replace=True,
        ),
    )

    # The bundle ships the metrics exporter and its Service but no
    # ServiceMonitor, so its chi_clickhouse_* series (replication, parts,
    # disks, per-table sizes for every CHI host) were served and never
    # scraped. ch-metrics sets its own `namespace` label to the CHI's
    # namespace; honorLabels keeps that instead of overwriting it with
    # kube-system, which is what lets alerts on these series route by the
    # namespace ClickHouse actually runs in.
    kubernetes.apiextensions.CustomResource(
        f"{cluster_name}-clickhouse-operator-metrics",
        api_version="monitoring.coreos.com/v1",
        kind="ServiceMonitor",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="clickhouse-operator-metrics",
            namespace=CLICKHOUSE_OPERATOR_BUNDLE_NAMESPACE,
            labels={"release": "prometheus"},
        ),
        spec={
            "selector": {"matchLabels": {"app": "clickhouse-operator"}},
            "namespaceSelector": {"matchNames": [CLICKHOUSE_OPERATOR_BUNDLE_NAMESPACE]},
            "endpoints": [
                {
                    "port": "ch-metrics",
                    "interval": "30s",
                    "scrapeTimeout": "20s",
                    "honorLabels": True,
                },
                {
                    "port": "op-metrics",
                    "interval": "60s",
                    # Altinity's operator dashboard selects on app.
                    "relabelings": [
                        {
                            "sourceLabels": ["__meta_kubernetes_service_label_app"],
                            "targetLabel": "app",
                        }
                    ],
                },
            ],
        },
        opts=ResourceOptions(provider=k8s_provider, depends_on=[operator]),
    )

    return operator
