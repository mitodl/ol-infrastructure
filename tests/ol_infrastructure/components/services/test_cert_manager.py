"""Tests for the Certificate spec OLCertManagerCert renders.

The component is shared by every public certificate in the repo, so the
namespace-Issuer and duration options must leave the default (Let's Encrypt
ClusterIssuer, cert-manager's own duration) exactly as it was.
"""

from __future__ import annotations

import asyncio

import pulumi

# Python 3.14+ compatibility
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())


class K8sMocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        return [args.name + "_id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


pulumi.runtime.set_mocks(K8sMocks())

from ol_infrastructure.components.services.cert_manager import (  # noqa: E402
    OLCertManagerCert,
    OLCertManagerCertConfig,
)


def _rendered_spec(name: str, **overrides):
    config = OLCertManagerCertConfig(
        application_name=name,
        k8s_namespace="test-ns",
        dest_secret_name=f"{name}-tls",
        dns_names=[f"{name}.example.com"],
        **overrides,
    )
    return OLCertManagerCert(name, cert_config=config).certificate_resource.spec


@pulumi.runtime.test
def test_default_uses_letsencrypt_cluster_issuer_and_default_duration():
    def check(spec):
        assert spec["issuerRef"] == {
            "group": "cert-manager.io",
            "name": "letsencrypt-production",
            "kind": "ClusterIssuer",
        }
        assert "duration" not in spec

    return _rendered_spec("default-cert").apply(check)


@pulumi.runtime.test
def test_issuer_name_selects_a_namespace_issuer():
    def check(spec):
        assert spec["issuerRef"] == {
            "group": "cert-manager.io",
            "name": "private-ca",
            "kind": "Issuer",
        }

    return _rendered_spec("private-cert", issuer_name="private-ca").apply(check)


@pulumi.runtime.test
def test_duration_is_rendered_when_set():
    def check(spec):
        assert spec["duration"] == "87600h"

    return _rendered_spec("long-cert", duration="87600h").apply(check)
