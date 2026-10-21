"""Tests for what the omnigraph 0.13 entrypoint needs to hand the lock over.

The entrypoint (agent-kit ``docker/omnigraph_server_entrypoint.py``) exits
before serving if it cannot read the Lease, list its peers, or find its own pod
behind the selector it was given, so each of those is a full data-tier outage
that only shows up on the first boot after a cutover. The names and grants are
pinned here against the contract that file documents.
"""

import pulumi
import pytest

from ol_infrastructure.applications.omnigraph.lock_handoff import (
    create_lock_handoff,
    uses_persisted_lock,
    validate_graph_removal_allowlist,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo
from tests.ol_infrastructure.recording_mocks import RecordingMocks


@pytest.mark.parametrize(
    ("internal_schema_version", "expected"),
    [(None, False), (9, False), (13, False), (14, True), (15, True)],
)
def test_the_lock_model_follows_the_served_format(
    internal_schema_version: int | None, *, expected: bool
) -> None:
    """0.11 (format 9) releases its lock on exit and its `force-unlock` fails
    on a missing lock, so none of this may reach an environment before its
    cutover to format 14.
    """
    assert uses_persisted_lock(internal_schema_version) is expected


def test_an_allowlisted_graph_that_is_still_declared_is_refused() -> None:
    """A stale entry would let a later accidental removal delete the graph."""
    with pytest.raises(ValueError, match="code-github-com-mitodl-ol-django"):
        validate_graph_removal_allowlist(
            ["code-github-com-mitodl-ol-django"],
            ["council", "code-bridge", "code-github-com-mitodl-ol-django"],
        )


@pytest.mark.parametrize("malformed", ["code-a", "", {}, 0, False, [1]])
def test_an_allowlist_that_is_not_a_list_of_ids_is_refused(malformed: object) -> None:
    """`pulumi config set` without `--path` stores a bare string.

    The falsey values are here because the callers default only a missing
    key, so these reach the check rather than reading as an empty list.
    """
    with pytest.raises(ValueError, match="must be a list"):
        validate_graph_removal_allowlist(malformed, ["council"])


def test_an_allowlisted_graph_that_is_no_longer_declared_passes() -> None:
    """The entry and the removal arrive in the same change."""
    assert validate_graph_removal_allowlist(
        ["code-github-com-mitodl-ol-django"], ["council", "code-bridge"]
    ) == ["code-github-com-mitodl-ol-django"]


@pulumi.runtime.test
def test_the_server_gets_the_lease_its_peers_and_nothing_else():
    """One named Lease to take, the pod list to check, and no way to scale."""
    mocks = RecordingMocks()
    pulumi.runtime.set_mocks(mocks, preview=False)

    handoff = create_lock_handoff(
        stack_info=StackInfo(
            name="CI",
            namespace="",
            env_suffix="ci",
            env_prefix="",
            full_name="organization/ol-application-omnigraph/CI",
            k8s_name="ci",
        ),
        namespace="omnigraph",
        k8s_global_labels={},
        service_account_name="omnigraph-server",
        pod_selector={"app.kubernetes.io/name": "omnigraph-server"},
        apply_actor="svc-witan-admin",
        graph_removal_allowlist=["code-a", "code-b"],
    )

    def check(_: object) -> None:
        (lease,) = mocks.of_type("kubernetes:coordination.k8s.io/v1:Lease")
        assert lease["metadata"]["name"] == "omnigraph-unlock"
        # No holder: the first boot takes it with an update.
        assert "spec" not in lease

        (role,) = mocks.of_type("kubernetes:rbac.authorization.k8s.io/v1:Role")
        assert role["rules"] == [
            {
                "apiGroups": ["coordination.k8s.io"],
                "resources": ["leases"],
                "resourceNames": ["omnigraph-unlock"],
                "verbs": ["get", "update"],
            },
            {"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]},
        ]
        (binding,) = mocks.of_type(
            "kubernetes:rbac.authorization.k8s.io/v1:RoleBinding"
        )
        assert binding["subjects"] == [
            {
                "kind": "ServiceAccount",
                "name": "omnigraph-server",
                "namespace": "omnigraph",
            }
        ]

    # Nothing orders the Lease against the binding, so the binding's URN alone
    # can resolve before the mocks have seen the Lease.
    return pulumi.Output.all(handoff.lease.urn, handoff.role_binding.urn).apply(check)


@pulumi.runtime.test
def test_the_env_is_what_the_entrypoint_reads() -> None:
    """The names are the entrypoint's contract; a typo is a refused boot."""
    pulumi.runtime.set_mocks(RecordingMocks(), preview=False)
    handoff = create_lock_handoff(
        stack_info=StackInfo(
            name="CI",
            namespace="",
            env_suffix="ci",
            env_prefix="",
            full_name="organization/ol-application-omnigraph/CI",
            k8s_name="ci",
        ),
        namespace="omnigraph",
        k8s_global_labels={},
        service_account_name="omnigraph-server",
        pod_selector={"app.kubernetes.io/name": "omnigraph-server"},
        apply_actor="svc-witan-admin",
        graph_removal_allowlist=[],
    )

    assert [e.name for e in handoff.env] == [
        "OMNIGRAPH_UNLOCK_LEASE",
        "OMNIGRAPH_POD_SELECTOR",
        "POD_NAME",
        "OMNIGRAPH_APPLY_ACTOR",
        "OMNIGRAPH_GRAPH_REMOVAL_ALLOWLIST",
    ]
    values = {e.name: e.value for e in handoff.env}
    assert values["OMNIGRAPH_UNLOCK_LEASE"] == "omnigraph-unlock"
    assert values["OMNIGRAPH_POD_SELECTOR"] == "app.kubernetes.io/name=omnigraph-server"
    assert values["OMNIGRAPH_APPLY_ACTOR"] == "svc-witan-admin"
    assert values["OMNIGRAPH_GRAPH_REMOVAL_ALLOWLIST"] == ""
    pod_name = next(e for e in handoff.env if e.name == "POD_NAME")
    assert pod_name.value_from.field_ref.field_path == "metadata.name"
