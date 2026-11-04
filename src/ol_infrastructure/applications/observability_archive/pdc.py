# ruff: noqa: E501
"""Grafana Cloud Private Data Source Connect for the archive's read path (D8, D9).

The PDC network, its agent token and the agent Deployment all live here, so one
`pulumi up` builds the whole read path. The agent runs in the archive namespace
and the Cloud datasources (Phases 1 and 2) use in-cluster Service URLs, so
nothing on the read path is exposed; the SSH tunnel is further restricted to the
two query-frontends.
"""

from typing import Any

import pulumi_kubernetes as kubernetes
import pulumiverse_grafana as grafana
from pulumi import InvokeOptions, ResourceOptions
from pulumiverse_grafana import cloud

from bridge.lib.versions import PDC_AGENT_VERSION
from ol_infrastructure.lib.aws.eks_helper import cached_image_uri
from ol_infrastructure.lib.ol_types import AlertTier, Component, K8sGlobalLabels
from ol_infrastructure.lib.pulumi_helper import StackInfo

# In-cluster read endpoints the Cloud datasources will point at. The tunnel only
# permits these two destinations.
LOKI_QUERY_FRONTEND_PORT = 3100
MIMIR_QUERY_FRONTEND_PORT = 8080
PDC_AGENT_METRICS_PORT = 8090
# Two agents is the floor for a rolling restart not breaking queries (D8).
PDC_AGENT_REPLICAS = 2
PDC_AGENT_CONNECTIONS = 2


def query_frontend_hosts(namespace: str) -> dict[str, str]:
    """Return each backend's in-cluster host:port for the Cloud datasources."""
    return {
        "loki": f"loki-query-frontend.{namespace}.svc.cluster.local:{LOKI_QUERY_FRONTEND_PORT}",
        "mimir": f"mimir-query-frontend.{namespace}.svc.cluster.local:{MIMIR_QUERY_FRONTEND_PORT}",
    }


def build(  # noqa: PLR0913
    *,
    stack_info: StackInfo,
    grafana_secrets: dict[str, Any],
    grafana_cloud_stack_slug: str,
    namespace: str,
    k8s_labels: K8sGlobalLabels,
    node_selector: dict[str, str],
    tolerations: list[dict[str, str]],
) -> cloud.PrivateDataSourceConnectNetwork:
    """Create the PDC network and token in Grafana Cloud and run the agent."""
    env = stack_info.env_suffix
    grafana_provider = grafana.Provider(
        "grafana-cloud-provider",
        url=grafana_secrets["grafana_url"],
        auth=grafana_secrets["grafana_api_token"],
        cloud_access_policy_token=grafana_secrets["cloud_access_policy_token"],
    )
    provider_opts = ResourceOptions(provider=grafana_provider)

    # Four identifiers for one stack: config names it by slug, the PDC API wants
    # the region slug ("us"), the agent wants the cluster slug
    # ("prod-us-central-0"), and both PDC network and agent use the numeric
    # instance id. All come from one lookup.
    cloud_stack = cloud.get_stack(
        slug=grafana_cloud_stack_slug,
        opts=InvokeOptions(provider=grafana_provider),
    )

    pdc_network = cloud.PrivateDataSourceConnectNetwork(
        f"telemetry-archive-pdc-network-{env}",
        name=f"telemetry-archive-{env}",
        display_name=f"Telemetry archive ({env})",
        region=cloud_stack.region_slug,
        stack_identifier=cloud_stack.id,
        opts=provider_opts,
    )
    pdc_token = cloud.PrivateDataSourceConnectNetworkToken(
        f"telemetry-archive-pdc-token-{env}",
        name=f"telemetry-archive-{env}-agent",
        display_name=f"Telemetry archive agent ({env})",
        pdc_network_id=pdc_network.pdc_network_id,
        region=cloud_stack.region_slug,
        opts=provider_opts,
    )

    agent_labels = k8s_labels.workload_labels(
        alert_tier=AlertTier.ticket, component=Component.agent
    )
    selector_labels = {"app.kubernetes.io/name": "pdc-agent"}
    # Deliberately unnamed (Pulumi auto-naming). The agent reads the token from
    # its environment once at startup and re-signs its SSH certificate with that
    # same value for the rest of its life, so a rotated token has to roll the
    # pods. pulumi-kubernetes already replaces a Secret whose data changes
    # (provider/pkg/provider/diff.go, v4.34.2); auto-naming makes that
    # replacement a new name, which changes the pod template below and rolls
    # the Deployment. A fixed name would be replaced in place and roll nothing.
    token_secret = kubernetes.core.v1.Secret(
        f"telemetry-archive-pdc-token-{env}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            namespace=namespace,
            labels=agent_labels,
        ),
        string_data={"token": pdc_token.token},
    )

    hosts = query_frontend_hosts(namespace)
    kubernetes.apps.v1.Deployment(
        f"telemetry-archive-pdc-agent-{env}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="pdc-agent",
            namespace=namespace,
            labels=agent_labels,
        ),
        spec=kubernetes.apps.v1.DeploymentSpecArgs(
            replicas=PDC_AGENT_REPLICAS,
            selector=kubernetes.meta.v1.LabelSelectorArgs(match_labels=selector_labels),
            strategy=kubernetes.apps.v1.DeploymentStrategyArgs(
                type="RollingUpdate",
                rolling_update=kubernetes.apps.v1.RollingUpdateDeploymentArgs(
                    max_unavailable=0, max_surge=1
                ),
            ),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    labels={**agent_labels, **selector_labels},
                ),
                spec=kubernetes.core.v1.PodSpecArgs(
                    node_selector=node_selector,
                    tolerations=[
                        kubernetes.core.v1.TolerationArgs(
                            key=toleration["key"],
                            operator=toleration["operator"],
                            value=toleration["value"],
                            effect=toleration["effect"],
                        )
                        for toleration in tolerations
                    ],
                    affinity=kubernetes.core.v1.AffinityArgs(
                        pod_anti_affinity=kubernetes.core.v1.PodAntiAffinityArgs(
                            preferred_during_scheduling_ignored_during_execution=[
                                kubernetes.core.v1.WeightedPodAffinityTermArgs(
                                    weight=100,
                                    pod_affinity_term=kubernetes.core.v1.PodAffinityTermArgs(
                                        label_selector=kubernetes.meta.v1.LabelSelectorArgs(
                                            match_labels=selector_labels
                                        ),
                                        topology_key="kubernetes.io/hostname",
                                    ),
                                )
                            ]
                        )
                    ),
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="pdc-agent",
                            image=f"{cached_image_uri('grafana/pdc-agent')}:{PDC_AGENT_VERSION}",
                            args=[
                                "-cluster",
                                cloud_stack.cluster_slug,
                                "-gcloud-hosted-grafana-id",
                                cloud_stack.id,
                                "-connections",
                                str(PDC_AGENT_CONNECTIONS),
                                # One `-o Name=value` per flag; the agent splits
                                # on the first space and requires exactly one
                                # `=`, so the space-separated host list is the
                                # option's value.
                                f"-ssh-flag=-o PermitRemoteOpen={hosts['loki']} {hosts['mimir']}",
                                "-metrics-addr",
                                f":{PDC_AGENT_METRICS_PORT}",
                            ],
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="GCLOUD_PDC_SIGNING_TOKEN",
                                    value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                        secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                            name=token_secret.metadata.name,
                                            key="token",
                                        )
                                    ),
                                )
                            ],
                            ports=[
                                kubernetes.core.v1.ContainerPortArgs(
                                    name="metrics",
                                    container_port=PDC_AGENT_METRICS_PORT,
                                )
                            ],
                            resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                requests={"cpu": "50m", "memory": "64Mi"},
                                limits={"memory": "256Mi"},
                            ),
                            # Ready means "a tunnel is up", so max_unavailable=0
                            # keeps the old agents until a replacement can serve
                            # queries. The agent has no health endpoint (its
                            # metrics mux serves only /metrics, and Grafana's own
                            # manifests ship no probe), but it only starts that
                            # server after the token has signed its SSH key, and
                            # it records a time_to_connect sample when the PDC
                            # gateway banner arrives, so a count of at least one
                            # is a completed handshake (pdc-agent v0.0.67,
                            # cmd/pdc/main.go and pkg/ssh/metrics.go). The image
                            # is alpine, so busybox wget and grep are present.
                            readiness_probe=kubernetes.core.v1.ProbeArgs(
                                exec_=kubernetes.core.v1.ExecActionArgs(
                                    command=[
                                        "sh",
                                        "-c",
                                        f"wget -qO- http://127.0.0.1:{PDC_AGENT_METRICS_PORT}/metrics"
                                        " | grep -Eq '^pdc_agent_ssh_time_to_connect_seconds_count"
                                        r"\{[^}]*\} [1-9]'",
                                    ]
                                ),
                                period_seconds=5,
                            ),
                        )
                    ],
                ),
            ),
        ),
    )
    return pdc_network
