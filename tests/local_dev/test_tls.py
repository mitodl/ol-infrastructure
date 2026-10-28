"""Render the core stack's TLS module under mocks."""

import asyncio
import importlib
from pathlib import Path
from typing import Any

import certifi
import pulumi
import pytest
import yaml

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

REPO = Path(__file__).parents[2]
CONFIGMAP = "kubernetes:core/v1:ConfigMap"
MKCERT_ROOT = "-----BEGIN CERTIFICATE-----\nmkcert\n-----END CERTIFICATE-----\n"


class RecordingMocks(pulumi.runtime.Mocks):
    def __init__(self):
        self.resources: list[tuple[str, dict[str, Any]]] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources.append((args.typ, args.inputs))
        return [f"{args.name}-id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


@pytest.fixture
def infra_modules(monkeypatch):
    """Import the core stack's modules as it does, a top-level package."""
    monkeypatch.syspath_prepend(str(REPO / "local-dev/infra"))
    return importlib.import_module("modules.tls"), importlib.import_module(
        "modules.namespaces"
    )


def _render(tmp_path, infra_modules, enabled_apps=()):
    tls, namespaces_module = infra_modules
    for name, text in (("tls.crt", "cert"), ("tls.key", "key")):
        (tmp_path / name).write_text(text)
    (tmp_path / "rootCA.pem").write_text(MKCERT_ROOT)
    mocks = RecordingMocks()
    pulumi.runtime.set_mocks(mocks, project="ol-local-dev-core", stack="Dev")

    @pulumi.runtime.test
    def run():
        namespaces = {
            name: pulumi.CustomResource("kubernetes:core/v1:Namespace", f"ns-{name}")
            for name in (
                "local-infra",
                "operations",
                *namespaces_module.app_namespaces_for(enabled_apps),
            )
        }
        tls.create_tls_resources(
            pulumi.ResourceOptions,
            namespaces=namespaces,
            cert_path=tmp_path / "tls.crt",
            key_path=tmp_path / "tls.key",
            ca_cert_path=tmp_path / "rootCA.pem",
            enabled_apps=enabled_apps,
        )
        return pulumi.Output.from_input(True)  # noqa: FBT003

    run()
    return mocks


def test_every_app_namespace_gets_the_public_and_mkcert_bundle(tmp_path, infra_modules):
    tls, namespaces_module = infra_modules
    enabled = ("ocw-studio",)
    mocks = _render(tmp_path, infra_modules, enabled_apps=enabled)
    bundles = {
        inputs["metadata"]["namespace"]: inputs["data"]
        for typ, inputs in mocks.resources
        if typ == CONFIGMAP and inputs["metadata"]["name"] == tls.CA_BUNDLE_CONFIGMAP
    }
    assert set(bundles) == set(namespaces_module.app_namespaces_for(enabled))
    assert "ocw-studio" in bundles
    public_roots = Path(certifi.where()).read_text()
    for data in bundles.values():
        assert set(data) == {tls.CA_BUNDLE_FILENAME}
        bundle = data[tls.CA_BUNDLE_FILENAME]
        assert bundle.startswith(public_roots)
        assert bundle.endswith(MKCERT_ROOT)


def test_apisix_trusts_the_same_bundle_the_apps_do(tmp_path, infra_modules):
    tls, _ = infra_modules
    mocks = _render(tmp_path, infra_modules)
    (apisix,) = [
        inputs
        for typ, inputs in mocks.resources
        if typ == "kubernetes:core/v1:Secret"
        and inputs["metadata"]["name"] == "apisix-trusted-ca"
    ]
    app_bundle = next(
        inputs["data"][tls.CA_BUNDLE_FILENAME]
        for typ, inputs in mocks.resources
        if typ == CONFIGMAP and inputs["metadata"]["name"] == tls.CA_BUNDLE_CONFIGMAP
    )
    # The provider marks stringData secret; mocks see it wrapped.
    string_data = apisix["stringData"]
    string_data = string_data.get("value", string_data)
    assert string_data[tls.APISIX_TRUSTED_CA_FILENAME] == app_bundle


def test_concourse_worker_mounts_the_bundle_as_its_certs_dir(infra_modules):
    """concourse.yaml is hand-written, so nothing else ties it to tls.py."""
    tls, _ = infra_modules
    manifest = (REPO / "local-dev/apps/ocw-studio/concourse.yaml").read_text()
    pods = [
        doc["spec"]["template"]["spec"]
        for doc in yaml.safe_load_all(manifest)
        if doc and doc["kind"] == "Deployment"
    ]
    ((pod, container),) = [
        (pod, container)
        for pod in pods
        for container in pod["containers"]
        if any(env["name"] == "CONCOURSE_CERTS_DIR" for env in container.get("env", []))
    ]
    certs_dir = next(
        env["value"] for env in container["env"] if env["name"] == "CONCOURSE_CERTS_DIR"
    )
    (mount,) = [m for m in container["volumeMounts"] if m["mountPath"] == certs_dir]
    (volume,) = [v for v in pod["volumes"] if v["name"] == mount["name"]]
    assert volume["configMap"]["name"] == tls.CA_BUNDLE_CONFIGMAP
    # Resource containers read the copy at /etc/ssl/certs/ca-certificates.crt.
    assert tls.CA_BUNDLE_FILENAME == "ca-certificates.crt"
