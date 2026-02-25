"""Install the Altinity ClickHouse Operator on the data EKS cluster.

The Altinity Operator manages ClickHouseInstallation and
ClickHouseKeeperInstallation CRDs. It is installed via the upstream raw YAML
manifest so that the version can be pinned precisely and renovate can track
releases via the github-releases datasource.
"""

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, StackReference

from bridge.lib.versions import CLICKHOUSE_OPERATOR_VERSION
from ol_infrastructure.lib.aws.eks_helper import check_cluster_namespace

CLICKHOUSE_OPERATOR_MANIFEST_URL = (
    "https://github.com/Altinity/clickhouse-operator/releases/download/"
    f"{CLICKHOUSE_OPERATOR_VERSION}/clickhouse-operator-install-bundle.yaml"
)
CLICKHOUSE_OPERATOR_NAMESPACE = "clickhouse-operator"


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

    return kubernetes.yaml.v2.ConfigGroup(
        f"{cluster_name}-clickhouse-operator",
        files=[CLICKHOUSE_OPERATOR_MANIFEST_URL],
        opts=ResourceOptions(
            provider=k8s_provider,
            delete_before_replace=True,
        ),
    )
