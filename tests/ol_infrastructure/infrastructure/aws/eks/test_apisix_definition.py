"""APISIX chart values shared by the EKS clusters and local-dev."""

from typing import Any

import pytest
from pydantic import ValidationError

from bridge.lib.versions import APISIX_CHART_VERSION
from ol_infrastructure.infrastructure.aws.eks.apisix_definition import (
    ApisixGatewayConfig,
    ApisixGatewayService,
    ApisixHighAvailability,
    ApisixTrustedCA,
    apisix_helm_values,
    http2_safe_chart,
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
        **{
            "high_availability": None,
            "otel_collector_address": None,
            "service_monitor_enabled": False,
            "admission_webhook_enabled": True,
            "install_gateway_api_crds": True,
            "http2_enabled": True,
            **overrides,
        },
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
        otel_collector_address="collector.example:4318",
        service_monitor_enabled=True,
        admission_webhook_enabled=True,
        install_gateway_api_crds=False,
        http2_enabled=True,
    )


def test_gateway_proxy_admin_key_comes_from_apisix_credentials():
    # Chart 3.0.0+ renders the GatewayProxy from apisix.admin.credentials;
    # a second copy under gatewayProxy would make the controller chart
    # render its own and fight the apisix chart's.
    for config in (_single_pod(), _fleet()):
        gateway_proxy = apisix_helm_values(config)["ingress-controller"]["gatewayProxy"]
        assert "createDefault" not in gateway_proxy
        assert "provider" not in gateway_proxy


def test_gateway_api_crds_follow_config():
    # The fleet's Gateway API CRDs are Traefik's experimental bundle.
    for config, expected in ((_single_pod(), True), (_fleet(), False)):
        crds = apisix_helm_values(config)["ingress-controller"]["crds"]
        assert crds == {"gatewayAPI": {"enabled": expected}}


def test_http_redirects_go_to_443_everywhere():
    # hq#10999: without it, http_to_https sends clients to APISIX's 9443.
    for config in (_single_pod(), _fleet()):
        values = apisix_helm_values(config)
        assert values["apisix"]["pluginAttrs"]["redirect"] == {"https_port": 443}


def test_http2_follows_config():
    for enabled in (True, False):
        values = apisix_helm_values(_single_pod(http2_enabled=enabled))
        assert values["apisix"]["enableHTTP2"] is enabled


def test_http2_only_on_charts_past_the_nginx_rce():
    # CVE-2026-123007 / CVE-2026-123633: a rollback through the APISIX_CHART
    # override to a 3.20.x-or-older chart must turn HTTP/2 back off.
    assert not http2_safe_chart("2.19.0")
    assert not http2_safe_chart("3.0.0")
    assert http2_safe_chart("3.1.0")
    assert http2_safe_chart("3.2.0")
    assert http2_safe_chart(APISIX_CHART_VERSION)


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
    # No collector address, no tracing.
    assert values["apisix"]["tracing"] is False
    assert "opentelemetry" not in values["apisix"]["pluginAttrs"]
    assert "pluginMetadata" not in values["ingress-controller"]["gatewayProxy"]


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
    collector = gateway_proxy["pluginMetadata"]["opentelemetry"]["collector"]
    assert collector["address"] == "collector.example:4318"
    assert values["apisix"]["pluginAttrs"]["opentelemetry"]["collector"] == collector
    assert values["apisix"]["tracing"] is True
    assert values["metrics"]["serviceMonitor"]["labels"] == {"ol.mit.edu/stack": "qa"}


def test_fleet_gateway_stays_off_spot():
    values = apisix_helm_values(_fleet())

    terms = values["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"]
    assert terms == [
        {
            "matchExpressions": [
                {
                    "key": "karpenter.sh/capacity-type",
                    "operator": "NotIn",
                    "values": ["spot"],
                }
            ]
        }
    ]
    assert "podAntiAffinity" in values["affinity"]
    # The ingress controller is not on the request path and may still use spot.
    controller = values["ingress-controller"]["deployment"]
    assert "nodeAffinity" not in controller.get("affinity", {})


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


def test_admission_webhook_is_on_unless_turned_off():
    assert apisix_helm_values(_fleet())["ingress-controller"]["webhook"] == {
        "enabled": True
    }
    assert apisix_helm_values(_single_pod(admission_webhook_enabled=False))[
        "ingress-controller"
    ]["webhook"] == {"enabled": False}


def test_cluster_capabilities_must_be_stated():
    with pytest.raises(ValidationError) as raised:
        ApisixGatewayConfig(
            env_suffix="dev",
            admin_key="admin",  # pragma: allowlist secret
            viewer_key="viewer",  # pragma: allowlist secret
            service=ApisixGatewayService(type="NodePort"),
        )
    missing = {error["loc"][0] for error in raised.value.errors()}
    assert missing == {
        "high_availability",
        "otel_collector_address",
        "service_monitor_enabled",
        "admission_webhook_enabled",
        "install_gateway_api_crds",
        "http2_enabled",
    }
