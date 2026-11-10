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

from bridge.lib.constants import CLICKHOUSE_NAMESPACE
from bridge.lib.magic_numbers import CLICKHOUSE_TERMINATION_GRACE_SECONDS
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
# reconcile.statefulSet.update.timeout bounds every wait on a StatefulSet,
# including the wait for a pod to go away when the operator recreates one.
# From 0.27.4 a pod that outlasts it is force-deleted (grace 0), so the bundle
# default of 300s would SIGKILL a ClickHouse server still finishing the daily
# backup inside its grace period. Only the operator-wide value counts: the
# operator builds its pollers from chop.Config(), not from the CHI. It applies
# to Keeper and every ClickHouse install too, so a genuinely failed update
# holds that reconcile this long before giving up. The CRD caps it at 3600.
OPERATOR_STATEFULSET_UPDATE_TIMEOUT_SECONDS = CLICKHOUSE_TERMINATION_GRACE_SECONDS + 600
# Objects the operator reads at startup, applied before its Deployment rolls.
_OPERATOR_PREREQUISITE_KINDS = frozenset(
    {"CustomResourceDefinition", "ConfigMap", "ClickHouseOperatorConfiguration"}
)


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


def _operator_configuration() -> dict[str, Any]:
    """Build the ClickHouseOperatorConfiguration merged over the bundle's config.yaml.

    The operator lists every ClickHouseOperatorConfiguration in its own
    namespace at startup and merges them, in name order, over the file config.

    The watch is narrowed to the namespace holding every CHI and CHK. With the
    bundle's empty ``include``, the Keeper controller's cache is keyed on the
    all-namespaces key ``""``, and the namespaced lists of a Keeper's
    StatefulSets, Services, ConfigMaps, Secrets, PVCs and PDBs fail with
    "unknown namespace for the cache" (Altinity/clickhouse-operator#2117). The
    CHK reconcile still completes, but discovery comes back empty, so a Keeper
    scale-down would leave the removed host's objects behind, and deleting the
    CHK would leave its StatefulSets, ConfigMaps and Services. A literal
    namespace name gives the cache that exact key; the bundle's own ``include``
    is empty, so the merged list is this one name. A CHI or CHK created in any
    other namespace is ignored.

    :returns: The ClickHouseOperatorConfiguration manifest.
    :rtype: dict[str, Any]
    """
    return {
        "apiVersion": "clickhouse.altinity.com/v1",
        "kind": "ClickHouseOperatorConfiguration",
        "metadata": {
            "name": "ol-overrides",
            "namespace": CLICKHOUSE_OPERATOR_BUNDLE_NAMESPACE,
        },
        "spec": {
            "watch": {"namespaces": {"include": [CLICKHOUSE_NAMESPACE]}},
            "reconcile": {
                "statefulSet": {
                    "update": {"timeout": OPERATOR_STATEFULSET_UPDATE_TIMEOUT_SECONDS}
                }
            },
        },
    }


def _resource_ref(obj: dict[str, Any]) -> str:
    """Format a manifest as a ``config.kubernetes.io/depends-on`` reference.

    :param obj: A Kubernetes manifest.
    :returns: ``<group>/namespaces/<ns>/<kind>/<name>``, or
        ``<group>/<kind>/<name>`` for a cluster-scoped object. The core group is
        the empty string.
    :rtype: str
    """
    group = obj["apiVersion"].rpartition("/")[0]
    metadata = obj["metadata"]
    scope = f"namespaces/{metadata['namespace']}/" if "namespace" in metadata else ""
    return f"{group}/{scope}{obj['kind']}/{metadata['name']}"


def _prepare_operator_deployment(objs: list[dict[str, Any]]) -> None:
    """Make the operator Deployment roll last, and alone.

    The CRDs, the config ConfigMaps and the ClickHouseOperatorConfiguration go
    in first, so a new operator never starts against old CRDs or config. The
    ConfigGroup's own heuristics only order CRDs before custom resources, not
    before a Deployment. A rolling update with no surge narrows the overlap
    between the old and new operator to the old pod's shutdown, but doesn't
    prevent it: a terminating pod doesn't count against maxUnavailable, so the
    new pod can start while the old one is still inside its grace period (the
    bundle sets none, so the default 30s). The operator has no leader election,
    so both can reconcile during that window. It stays type RollingUpdate
    because a server-side apply cannot drop the defaulted rollingUpdate block
    that type Recreate forbids.

    The CRDs stay in this ConfigGroup: a child's URN is
    ``<group name>:<namespace>/<name>``, so moving them to another group would
    delete and recreate them, and deleting a CRD deletes every CHI and CHK.

    :param objs: The bundle manifests, modified in place.
    """
    deployment = next(obj for obj in objs if obj["kind"] == "Deployment")
    deployment["metadata"].setdefault("annotations", {})[
        "config.kubernetes.io/depends-on"
    ] = ",".join(
        _resource_ref(obj)
        for obj in objs
        if obj["kind"] in _OPERATOR_PREREQUISITE_KINDS
    )
    deployment["spec"]["strategy"] = {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
    }


def setup_clickhouse_operator(
    cluster_name: str,
    cluster_stack: StackReference,
    k8s_provider: kubernetes.Provider,
) -> kubernetes.yaml.v2.ConfigGroup | None:
    """Install the Altinity ClickHouse Operator into the clickhouse-operator namespace.

    The operator is installed from the upstream install-bundle manifest, which
    includes CRDs (ClickHouseInstallation, ClickHouseKeeperInstallation, etc.)
    and the operator Deployment. It watches only CLICKHOUSE_NAMESPACE (see
    ``_operator_configuration``).

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

    objs = [*_fetch_operator_manifests(), _operator_configuration()]
    _prepare_operator_deployment(objs)
    operator = kubernetes.yaml.v2.ConfigGroup(
        f"{cluster_name}-clickhouse-operator",
        objs=objs,
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
                    # Altinity's vendored operator dashboard hardcodes [1m] rate
                    # windows, which need two samples per minute.
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
