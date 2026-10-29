"""Pin how the deployed APISIX values differ from local-dev's.

Both releases get ``apisix_helm_values``; only the ``ApisixGatewayConfig`` each
caller builds should differ. A key added inline in ``setup_apisix`` (or in
local-dev's ``create_ingress``) shows up here as an undeclared difference.
"""

import importlib
from typing import Any

import pulumi
import pulumi_fastly as fastly
import pulumi_kubernetes as kubernetes
from pulumi.runtime import rpc

from bridge.lib.constants import mit_learn_session_cookie_name
from bridge.lib.versions import APISIX_CHART_VERSION
from ol_infrastructure.infrastructure.aws.eks import apisix_official
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import StackInfo
from tests.local_dev.test_ingress import _render
from tests.ol_infrastructure.recording_mocks import RecordingMocks

# Where applications.QA's values differ from local-dev's, as dotted paths. A
# path names a whole subtree when everything under it differs.
EXPECTED_DIFFERENCES = {
    # high_availability: an HPA-scaled fleet vs. local's single pod.
    "autoscaling",
    "replicaCount",
    "affinity",
    "topologySpreadConstraints",
    "updateStrategy",
    "minReadySeconds",
    "terminationGracePeriodSeconds",
    "lifecycle",
    "podDisruptionBudget",
    # The same for the ingress controller (ingress_controller_replicas 3 vs. 1).
    "ingress-controller.deployment.replicas",
    "ingress-controller.deployment.strategy",
    "ingress-controller.deployment.minReadySeconds",
    "ingress-controller.deployment.terminationGracePeriodSeconds",
    "ingress-controller.deployment.lifecycle",
    "ingress-controller.deployment.affinity",
    "ingress-controller.deployment.topologySpreadConstraints",
    # EKS schedules onto the tainted operations nodes; k3d has no taints.
    "tolerations",
    "ingress-controller.deployment.tolerations",
    # EKS pulls through the ECR Docker Hub cache; local uses the chart's image.
    "image",
    # apisix_memory is 1500Mi on applications.QA; local keeps the 400Mi
    # default request with a 1Gi limit.
    "resources.requests.memory",
    "resources.limits",
    # An NLB LoadBalancer vs. k3d's NodePorts 30080/30443.
    "service.type",
    "service.annotations",
    "service.http.nodePort",
    "service.tls.nodePort",
    # Stack labels replace the selector labels, which are immutable on an
    # existing Deployment; local keeps the chart's.
    "service.labelsOverride",
    # OpenTelemetry to the Grafana Alloy receiver, which k3d does not run.
    "apisix.plugins",
    "apisix.tracing",
    "apisix.pluginAttrs.opentelemetry",
    "ingress-controller.gatewayProxy.pluginMetadata",
    # Fastly's addresses as trusted X-Forwarded-For sources; nothing fronts k3d.
    "apisix.trustedAddresses",
    # ServiceMonitor needs the Prometheus operator CRD, absent on k3d.
    "metrics",
    # Local's mkcert CA for Keycloak, and the checksum that rolls the pod when
    # it changes.
    "apisix.ssl.existingCASecret",
    "apisix.ssl.certCAFilename",
    "podAnnotations",
    # The admission webhook denies local's batch-applied routes; see ingress.py.
    "ingress-controller.webhook",
}

EKS_ENV_SUFFIX = "qa"
EKS_CLUSTER_NAME = f"applications-{EKS_ENV_SUFFIX}"
# The session cookie name in the httpSrv snippet is the one value under
# apisix.nginx that follows env_suffix, so both sides swap it for this.
_SESSION_COOKIE_PLACEHOLDER = "<mit-learn-session-cookie>"

Path = tuple[str, ...]


class _EksMocks(RecordingMocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        resource_id, outputs = super().new_resource(args)
        # setup_apisix reads the gateway Service's load balancer hostname.
        if args.typ == "kubernetes:core/v1:Service" and args.resource_id:
            outputs["status"] = {"loadBalancer": {"ingress": [{"hostname": "nlb"}]}}
        return [resource_id, outputs]

    def call(self, args: pulumi.runtime.MockCallArgs):
        if args.token == "fastly:index/getFastlyIpRanges:getFastlyIpRanges":  # noqa: S105
            return {
                "cidrBlocks": ["192.0.2.0/24"],
                "ipv6CidrBlocks": ["2001:db8::/32"],
            }
        return {}


class _StandIn(pulumi.ComponentResource):
    """Stands in for the resources setup_apisix only orders against."""

    def __init__(self, name: str):
        super().__init__("test:index:StandIn", name)
        self.kubeconfig = "kubeconfig"


def _normalise(value: Any, session_cookie_name: str) -> Any:
    if isinstance(value, dict):
        # How the mocks hand back an input that holds a secret.
        if value.get(rpc._special_sig_key) == rpc._special_secret_sig:
            return _normalise(value["value"], session_cookie_name)
        return {k: _normalise(v, session_cookie_name) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalise(v, session_cookie_name) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        return value.replace(session_cookie_name, _SESSION_COOKIE_PLACEHOLDER)
    return value


def _flatten(value: Any, prefix: Path = ()) -> dict[Path, Any]:
    if isinstance(value, dict) and value:
        leaves: dict[Path, Any] = {}
        for key, child in value.items():
            leaves.update(_flatten(child, (*prefix, key)))
        return leaves
    return {prefix: value}


def differing_paths(deployed: dict[str, Any], local: dict[str, Any]) -> set[str]:
    """Return the paths at which two sets of values differ.

    A subtree is reported as one path when every leaf under it differs.
    """
    deployed_leaves = _flatten(deployed)
    local_leaves = _flatten(local)
    all_leaves = deployed_leaves.keys() | local_leaves.keys()
    differing = {
        leaf
        for leaf in all_leaves
        if leaf not in deployed_leaves
        or leaf not in local_leaves
        or deployed_leaves[leaf] != local_leaves[leaf]
    }

    def fully_differs(prefix: Path) -> bool:
        return all(
            leaf in differing for leaf in all_leaves if leaf[: len(prefix)] == prefix
        )

    collapsed = set()
    for leaf in differing:
        prefix = next(
            leaf[:n] for n in range(1, len(leaf) + 1) if fully_differs(leaf[:n])
        )
        collapsed.add(".".join(prefix))
    return collapsed


def _deployed_values(monkeypatch) -> dict[str, Any]:
    mocks = _EksMocks()
    pulumi.runtime.set_mocks(
        mocks, project="ol-infrastructure-eks", stack="applications.QA", preview=False
    )
    config = pulumi.runtime.config.CONFIG.get()
    # From Pulumi.applications.QA.yaml. The admin keys match what test_ingress
    # gives local, so the admin credentials compare equal.
    for key, value in {
        "eks:apisix_memory": "1500Mi",
        "eks:apisix_admin_key": "admin",  # pragma: allowlist secret
        "eks:apisix_viewer_key": "viewer",  # pragma: allowlist secret
        "eks:apisix_domains": '["api.rc.learn.mit.edu", "api.rc.mitxonline.mit.edu"]',
    }.items():
        monkeypatch.setitem(config, key, value)

    def stub_crds(resource_name: str, **_: Any) -> pulumi.Resource:
        # The real one downloads the chart to read its CRDs.
        return kubernetes.core.v1.Namespace(resource_name)

    monkeypatch.setattr(apisix_official, "adopt_helm_chart_crds", stub_crds)

    @pulumi.runtime.test
    def program():
        k8s_provider = kubernetes.Provider("k8s")
        operations = kubernetes.core.v1.Namespace("operations")
        stand_in = _StandIn("stand-in")
        stack_info = StackInfo(
            name="QA",
            namespace="applications",
            env_suffix=EKS_ENV_SUFFIX,
            env_prefix="applications",
            full_name="organization/ol-infrastructure-eks/applications.QA",
            k8s_name="ol-infrastructure-eks.applications.QA",
        )
        apisix_official.setup_apisix(
            cluster_name=EKS_CLUSTER_NAME,
            k8s_provider=k8s_provider,
            operations_namespace=operations,
            node_groups=[],
            gateway_api_crds=stand_in,
            stack_info=stack_info,
            k8s_global_labels={
                "pulumi_managed": "true",
                "pulumi_stack": "infrastructure.aws.eks.applications.QA",
            },
            operations_tolerations=[
                {
                    "key": "operations",
                    "operator": "Equal",
                    "value": "true",
                    "effect": "NoSchedule",
                },
            ],
            versions={"APISIX_CHART": APISIX_CHART_VERSION},
            eks_config=pulumi.Config("eks"),
            target_vpc=pulumi.Output.from_input(
                {"k8s_public_subnet_ids": ["subnet-a", "subnet-b"]}
            ),
            aws_config=AWSBase(
                tags={"OU": "operations", "Environment": EKS_CLUSTER_NAME}
            ),
            cluster=stand_in,
            lb_controller=stand_in,
            fastly_provider=fastly.Provider("fastly"),
            vpa_release=stand_in,
        )
        return pulumi.Output.from_input(None)

    program()
    (release,) = [
        r.inputs
        for r in mocks.resources
        if r.typ == "kubernetes:helm.sh/v3:Release"
        and r.name == f"{EKS_CLUSTER_NAME}-apisix-official-helm-release"
    ]
    return _normalise(release["values"], mit_learn_session_cookie_name(EKS_ENV_SUFFIX))


def _local_values(monkeypatch) -> dict[str, Any]:
    resources = _render(monkeypatch)
    values = resources[("kubernetes:helm.sh/v3:Release", "apisix")]["values"]
    local_env_suffix = importlib.import_module("modules.ingress").LOCAL_ENV_SUFFIX
    return _normalise(values, mit_learn_session_cookie_name(local_env_suffix))


def test_deployed_and_local_values_differ_only_where_declared(monkeypatch):
    """Fail on a difference not in EXPECTED_DIFFERENCES, or one that went away."""
    deployed = _deployed_values(monkeypatch)
    local = _local_values(monkeypatch)
    assert differing_paths(deployed, local) == EXPECTED_DIFFERENCES
