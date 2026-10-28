"""APISIX chart values shared by the EKS clusters and local-dev."""

from typing import Any

from ol_infrastructure.infrastructure.aws.eks.apisix_definition import (
    ApisixGatewayConfig,
    ApisixGatewayService,
    ApisixHighAvailability,
    ApisixTrustedCA,
    apisix_helm_values,
)

FLEET_ONLY_KEYS = {
    "topologySpreadConstraints",
    "affinity",
    "updateStrategy",
    "minReadySeconds",
    "terminationGracePeriodSeconds",
    "lifecycle",
    "podDisruptionBudget",
}


def _single_pod(**overrides: Any) -> ApisixGatewayConfig:
    return ApisixGatewayConfig(
        env_suffix="dev",
        admin_key="admin",  # pragma: allowlist secret
        viewer_key="viewer",  # pragma: allowlist secret
        service=ApisixGatewayService(
            type="NodePort", http_node_port=30080, tls_node_port=30443
        ),
        **overrides,
    )


def _fleet() -> ApisixGatewayConfig:
    return ApisixGatewayConfig(
        env_suffix="QA",
        admin_key="admin",  # pragma: allowlist secret
        viewer_key="viewer",  # pragma: allowlist secret
        service=ApisixGatewayService(type="LoadBalancer", annotations={}),
        high_availability=ApisixHighAvailability(
            min_replicas=3, max_replicas=5, target_cpu_utilization_percentage=50
        ),
        ingress_controller_replicas=3,
        image_repository="registry.example/apache/apisix",
        labels={"ol.mit.edu/stack": "qa"},
        trusted_addresses=["192.0.2.0/24"],
        otel_tracing_enabled=True,
        service_monitor_enabled=True,
    )


def test_http_redirects_go_to_443_everywhere():
    # hq#10999: without it, http_to_https sends clients to APISIX's 9443.
    for config in (_single_pod(), _fleet()):
        values = apisix_helm_values(config)
        assert values["apisix"]["pluginAttrs"]["redirect"] == {"https_port": 443}


def test_single_pod_has_no_fleet_settings():
    values = apisix_helm_values(_single_pod())

    assert values["autoscaling"] == {"enabled": False}
    assert values["replicaCount"] == 1
    assert not FLEET_ONLY_KEYS & values.keys()
    assert values["ingress-controller"]["deployment"]["replicas"] == 1
    assert "strategy" not in values["ingress-controller"]["deployment"]
    # Changing the chart's selector labels would hit the Deployment's
    # immutable selector on an existing release.
    assert "labelsOverride" not in values["service"]
    for absent in ("image", "metrics", "podAnnotations"):
        assert absent not in values
    assert "trustedAddresses" not in values["apisix"]
    assert "plugins" not in values["apisix"]


def test_fleet_settings():
    values = apisix_helm_values(_fleet())

    assert values["autoscaling"]["minReplicas"] == 3
    assert "replicaCount" not in values
    assert values.keys() >= FLEET_ONLY_KEYS
    assert values["service"]["labelsOverride"] == {
        "ol.mit.edu/stack": "qa",
        "app.kubernetes.io/name": "apache-apisix",
        "app.kubernetes.io/component": "gateway",
    }
    assert values["apisix"]["plugins"][-1] == "opentelemetry"
    gateway_proxy = values["ingress-controller"]["gatewayProxy"]
    assert "opentelemetry" in gateway_proxy["pluginMetadata"]
    assert values["metrics"]["serviceMonitor"]["labels"] == {"ol.mit.edu/stack": "qa"}


def test_node_ports_and_trusted_ca():
    values = apisix_helm_values(
        _single_pod(
            trusted_ca=ApisixTrustedCA(secret_name="ca", filename="ca-bundle.pem"),
            pod_annotations={"checksum/trusted-ca": "abc"},
        )
    )

    assert values["service"]["type"] == "NodePort"
    assert values["service"]["http"]["nodePort"] == 30080
    assert values["service"]["tls"]["nodePort"] == 30443
    assert (
        values["apisix"]["ssl"]
        | {
            "existingCASecret": "ca",  # pragma: allowlist secret
            "certCAFilename": "ca-bundle.pem",
        }
        == values["apisix"]["ssl"]
    )
    assert values["podAnnotations"] == {"checksum/trusted-ca": "abc"}


def test_memory_limit_defaults_to_double_the_request():
    assert apisix_helm_values(_single_pod(memory_request="1.5Gi"))["resources"][
        "limits"
    ] == {"memory": "3Gi"}
    assert apisix_helm_values(_single_pod(memory_limit="1Gi"))["resources"][
        "limits"
    ] == {"memory": "1Gi"}
