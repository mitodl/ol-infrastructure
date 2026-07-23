"""Tests for the OLApisixUpstream Pulumi component.

This module verifies:
1. OLApisixUpstreamConfig's chash field validation (hash_on/hash_key are
   required together with loadbalancer_type="chash", and rejected otherwise)
2. The rendered ApisixUpstream CRD spec for roundrobin vs chash
3. The ApisixUpstream resource is named after the target Service, per the
   apisix-ingress-controller name-matching contract documented on the class
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

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from ol_infrastructure.components.services.apisix import (  # noqa: E402
    OLApisixUpstream,
    OLApisixUpstreamConfig,
)

# ─── OLApisixUpstreamConfig validation ─────────────────────────────────────────


def test_chash_requires_hash_on_and_hash_key():
    with pytest.raises(ValidationError, match="chash load balancing requires"):
        OLApisixUpstreamConfig(
            service_name="myapp-service",
            k8s_namespace="myapp-ns",
            loadbalancer_type="chash",
        )


def test_chash_requires_hash_key_even_with_hash_on():
    with pytest.raises(ValidationError, match="chash load balancing requires"):
        OLApisixUpstreamConfig(
            service_name="myapp-service",
            k8s_namespace="myapp-ns",
            loadbalancer_type="chash",
            hash_on="vars",
        )


def test_chash_with_hash_on_and_hash_key_ok():
    cfg = OLApisixUpstreamConfig(
        service_name="myapp-service",
        k8s_namespace="myapp-ns",
        loadbalancer_type="chash",
        hash_on="vars",
        hash_key="remote_addr",
    )
    assert cfg.hash_on == "vars"
    assert cfg.hash_key == "remote_addr"


def test_roundrobin_rejects_hash_on():
    with pytest.raises(ValidationError, match="only meaningful when"):
        OLApisixUpstreamConfig(
            service_name="myapp-service",
            k8s_namespace="myapp-ns",
            loadbalancer_type="roundrobin",
            hash_on="vars",
        )


def test_roundrobin_rejects_hash_key():
    with pytest.raises(ValidationError, match="only meaningful when"):
        OLApisixUpstreamConfig(
            service_name="myapp-service",
            k8s_namespace="myapp-ns",
            loadbalancer_type="roundrobin",
            hash_key="remote_addr",
        )


def test_default_loadbalancer_type_is_roundrobin():
    cfg = OLApisixUpstreamConfig(
        service_name="myapp-service",
        k8s_namespace="myapp-ns",
    )
    assert cfg.loadbalancer_type == "roundrobin"
    assert cfg.hash_on is None
    assert cfg.hash_key is None


# ─── Rendered ApisixUpstream CRD spec ──────────────────────────────────────────


@pulumi.runtime.test
def test_roundrobin_spec_has_no_hash_fields():
    """A roundrobin upstream's rendered spec must not carry hashOn/key."""
    upstream = OLApisixUpstream(
        "test-roundrobin-upstream",
        OLApisixUpstreamConfig(
            service_name="myapp-service",
            k8s_namespace="myapp-ns",
        ),
    )

    def check(spec):
        assert spec == {"loadbalancer": {"type": "roundrobin"}}

    return upstream.apisix_upstream_resource.spec.apply(check)


@pulumi.runtime.test
def test_chash_spec_has_hash_on_and_key():
    """A chash upstream's rendered spec must carry the configured hashOn/key."""
    upstream = OLApisixUpstream(
        "test-chash-upstream",
        OLApisixUpstreamConfig(
            service_name="cms-edxapp-app",
            k8s_namespace="mitx-openedx",
            loadbalancer_type="chash",
            hash_on="vars",
            hash_key="remote_addr",
        ),
    )

    def check(spec):
        assert spec == {
            "loadbalancer": {
                "type": "chash",
                "hashOn": "vars",
                "key": "remote_addr",
            }
        }

    return upstream.apisix_upstream_resource.spec.apply(check)


@pulumi.runtime.test
def test_resource_name_matches_service_name():
    """apisix-ingress-controller matches ApisixUpstream to a Service by exact
    same-name, same-namespace lookup -- metadata.name must equal service_name,
    not the Pulumi resource name.
    """
    upstream = OLApisixUpstream(
        "test-name-matching-upstream",
        OLApisixUpstreamConfig(
            service_name="cms-edxapp-app",
            k8s_namespace="mitx-openedx",
            loadbalancer_type="chash",
            hash_on="vars",
            hash_key="remote_addr",
        ),
    )

    def check(metadata):
        assert metadata["name"] == "cms-edxapp-app"
        assert metadata["namespace"] == "mitx-openedx"

    return upstream.apisix_upstream_resource.metadata.apply(check)
