"""Tests for the make_vpa helper in ol_infrastructure.lib.k8s_vpa."""

from __future__ import annotations

import asyncio

import pulumi

# Python 3.14+ compatibility
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())


class VPAMocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        return [args.name + "_id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


pulumi.runtime.set_mocks(VPAMocks())

import pulumi_kubernetes as kubernetes  # noqa: E402

from ol_infrastructure.lib.k8s_vpa import make_vpa  # noqa: E402


def _fake_provider() -> kubernetes.Provider:
    return kubernetes.Provider("test-provider", kubeconfig="fake")


@pulumi.runtime.test
def test_make_vpa_update_mode():
    """VPA updateMode is always InPlaceOrRecreate."""
    vpa = make_vpa(
        name="test-vpa",
        namespace="default",
        target_kind="Deployment",
        target_name="my-deployment",
        controlled_resources=["cpu", "memory"],
        min_allowed={"cpu": "10m", "memory": "64Mi"},
        max_allowed={"cpu": "2000m", "memory": "4Gi"},
        k8s_provider=_fake_provider(),
    )

    def check(spec):
        assert spec["updatePolicy"]["updateMode"] == "InPlaceOrRecreate"

    return vpa.spec.apply(check)


@pulumi.runtime.test
def test_make_vpa_controlled_resources_cpu_and_memory():
    """VPA containerPolicies carry the requested controlled resources."""
    vpa = make_vpa(
        name="test-vpa-both",
        namespace="default",
        target_kind="Deployment",
        target_name="my-deployment",
        controlled_resources=["cpu", "memory"],
        min_allowed={"cpu": "10m", "memory": "64Mi"},
        max_allowed={"cpu": "2000m", "memory": "4Gi"},
        k8s_provider=_fake_provider(),
    )

    def check(spec):
        policies = spec["resourcePolicy"]["containerPolicies"]
        assert len(policies) == 1
        assert policies[0]["controlledResources"] == ["cpu", "memory"]

    return vpa.spec.apply(check)


@pulumi.runtime.test
def test_make_vpa_controlled_resources_memory_only():
    """VPA can be restricted to memory-only (for HPA co-existence)."""
    vpa = make_vpa(
        name="test-vpa-mem",
        namespace="default",
        target_kind="Deployment",
        target_name="my-deployment",
        controlled_resources=["memory"],
        min_allowed={"memory": "128Mi"},
        max_allowed={"memory": "4Gi"},
        k8s_provider=_fake_provider(),
    )

    def check(spec):
        policies = spec["resourcePolicy"]["containerPolicies"]
        assert policies[0]["controlledResources"] == ["memory"]

    return vpa.spec.apply(check)


@pulumi.runtime.test
def test_make_vpa_container_name_default():
    """ContainerName defaults to the wildcard when not specified."""
    vpa = make_vpa(
        name="test-vpa-wildcard",
        namespace="default",
        target_kind="Deployment",
        target_name="my-deployment",
        controlled_resources=["cpu", "memory"],
        min_allowed={"cpu": "10m", "memory": "64Mi"},
        max_allowed={"cpu": "2000m", "memory": "4Gi"},
        k8s_provider=_fake_provider(),
    )

    def check(spec):
        policies = spec["resourcePolicy"]["containerPolicies"]
        assert policies[0]["containerName"] == "*"

    return vpa.spec.apply(check)


@pulumi.runtime.test
def test_make_vpa_container_name_scoped():
    """ContainerName is passed through so bounds don't leak onto sidecars."""
    vpa = make_vpa(
        name="test-vpa-scoped",
        namespace="default",
        target_kind="Deployment",
        target_name="my-deployment",
        controlled_resources=["cpu", "memory"],
        container_name="my-app",
        min_allowed={"cpu": "10m", "memory": "64Mi"},
        max_allowed={"cpu": "2000m", "memory": "4Gi"},
        k8s_provider=_fake_provider(),
    )

    def check(spec):
        policies = spec["resourcePolicy"]["containerPolicies"]
        assert policies[0]["containerName"] == "my-app"

    return vpa.spec.apply(check)


@pulumi.runtime.test
def test_make_vpa_target_ref():
    """VPA targetRef reflects the provided kind and name."""
    vpa = make_vpa(
        name="test-vpa-ref",
        namespace="apps",
        target_kind="Deployment",
        target_name="my-app",
        controlled_resources=["cpu", "memory"],
        min_allowed={"cpu": "10m", "memory": "64Mi"},
        max_allowed={"cpu": "1000m", "memory": "2Gi"},
        k8s_provider=_fake_provider(),
    )

    def check(spec):
        ref = spec["targetRef"]
        assert ref["apiVersion"] == "apps/v1"
        assert ref["kind"] == "Deployment"
        assert ref["name"] == "my-app"

    return vpa.spec.apply(check)
