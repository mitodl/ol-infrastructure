"""Render local-dev's APISIX under mocks; pin what it shares with deployed."""

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any

import pulumi

from ol_infrastructure.infrastructure.aws.eks.apisix_definition import (
    gateway_class_spec,
    gateway_spec,
    identity_header_strip_rule_spec,
)

# Python 3.14+ compatibility, as test_mitxonline_program.py does.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

INFRA_DIR = Path(__file__).parents[2] / "local-dev/infra"


class RecordingMocks(pulumi.runtime.Mocks):
    def __init__(self):
        self.resources: dict[tuple[str, str], dict[str, Any]] = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources[(args.typ, args.name)] = args.inputs
        return [f"{args.name}-id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


def _render(
    monkeypatch, helm_values_calls: list[dict[str, Any]] | None = None
) -> dict[tuple[str, str], dict[str, Any]]:
    monkeypatch.syspath_prepend(str(INFRA_DIR))
    for name in [m for m in sys.modules if m == "modules" or m.startswith("modules.")]:
        monkeypatch.delitem(sys.modules, name)
    mocks = RecordingMocks()
    pulumi.runtime.set_mocks(mocks, preview=False)
    import pulumi_kubernetes as k8s  # noqa: PLC0415

    helpers = importlib.import_module("modules.helpers")
    ingress = importlib.import_module("modules.ingress")
    if helm_values_calls is not None:
        real_helm_values = ingress.apisix_helm_values

        def recording_helm_values(config):
            helm_values_calls.append(real_helm_values(config))
            return helm_values_calls[-1]

        monkeypatch.setattr(ingress, "apisix_helm_values", recording_helm_values)

    @pulumi.runtime.test
    def program():
        _k8s = helpers.make_resource_opts(k8s.Provider("k3d-local"))
        operations = k8s.core.v1.Namespace("operations")
        trusted_ca = k8s.core.v1.Secret(
            "apisix-trusted-ca", metadata={"name": "apisix-trusted-ca"}
        )
        ingress.create_ingress(
            _k8s,
            namespaces={"operations": operations},
            apisix_admin_key=pulumi.Output.secret("admin"),
            apisix_viewer_key=pulumi.Output.secret("viewer"),
            cert_manager_version="v1.0.0",
            apisix_version="2.18.2",
            trusted_ca_secret=trusted_ca,
            trusted_ca_checksum="abc",
        )
        return pulumi.Output.from_input(None)

    program()
    return mocks.resources


def test_apisix_values_come_from_the_deployed_definition(monkeypatch):
    helm_values_calls: list[dict[str, Any]] = []
    resources = _render(monkeypatch, helm_values_calls)
    values = resources[("kubernetes:helm.sh/v3:Release", "apisix")]["values"]
    # The admin keys are secret, so the mocks hand the whole values input
    # back wrapped as a secret.
    values = values["value"]

    # The release gets the shared function's output whole, so a hand-written
    # dict carrying the local keys below would fail here.
    assert len(helm_values_calls) == 1
    shared = helm_values_calls[0]
    assert values["apisix"]["nginx"] == shared["apisix"]["nginx"]
    assert (
        values["apisix"]["ssl"]["sslCiphers"] == shared["apisix"]["ssl"]["sslCiphers"]
    )
    # The mocks round-trip ints as floats and keep the admin key an Output,
    # so compare the shape here rather than the values.
    assert set(values) == set(shared)
    assert set(values["apisix"]) == set(shared["apisix"])
    assert set(values["ingress-controller"]) == set(shared["ingress-controller"])
    assert values["apisix"]["nginx"]["customLuaSharedDicts"]

    assert values["apisix"]["pluginAttrs"]["redirect"] == {"https_port": 443}
    assert values["service"]["type"] == "NodePort"
    assert values["service"]["http"]["nodePort"] == 30080
    assert values["service"]["tls"]["nodePort"] == 30443
    assert values["replicaCount"] == 1
    # The hand-written manifests race the webhook; see ingress.py.
    assert values["ingress-controller"]["webhook"] == {"enabled": False}
    assert values["autoscaling"] == {"enabled": False}
    assert (
        values["apisix"]["ssl"]
        | {
            "existingCASecret": "apisix-trusted-ca",  # pragma: allowlist secret
            "certCAFilename": "ca-bundle.pem",
        }
        == values["apisix"]["ssl"]
    )
    assert values["podAnnotations"] == {"checksum/trusted-ca": "abc"}
    # The httpSrv snippet's error_page serves from this mount.
    assert (
        values["extraVolumes"][0]["configMap"]["name"]
        == (
            resources[("kubernetes:core/v1:ConfigMap", "apisix-error-pages")][
                "metadata"
            ]["name"]
        )
    )


def test_gateway_and_global_rule_match_deployed(monkeypatch):
    resources = _render(monkeypatch)

    gateway_class = resources[
        ("kubernetes:gateway.networking.k8s.io/v1:GatewayClass", "apisix-gateway-class")
    ]
    gateway = resources[
        ("kubernetes:gateway.networking.k8s.io/v1:Gateway", "apisix-gateway")
    ]
    rule = resources[
        (
            "kubernetes:apisix.apache.org/v2:ApisixGlobalRule",
            "apisix-identity-header-strip-global-rule",
        )
    ]
    assert gateway_class["spec"] == gateway_class_spec()
    assert gateway["spec"] == gateway_spec()
    assert gateway["metadata"] == {"name": "apisix", "namespace": "operations"}
    assert rule["spec"] == identity_header_strip_rule_spec()
    assert rule["metadata"]["name"] == "identity-header-strip"
