"""Tests for the Cedar ``policies:`` block of the generated cluster.yaml.

The failure mode this guards is a graph that exists with no bundle governing
it. That is not "open" — omnigraph is default-deny, so an ungoverned graph
refuses every write with an error pointing at policy rather than at the missing
wiring, and it looks identical to a policy that is simply too strict. Deriving
the block from the graph map (rather than rebuilding it from ``managed_repos``)
is what makes the two impossible to drift apart, so that is what is pinned.

See agent-kit ``mcp/servers/witan/policy/README.md`` for the bundle contract.
"""

import pytest

from ol_infrastructure.applications.omnigraph.data_tier import (
    BRIDGE_GRAPH_ID,
    COUNCIL_GRAPH_ID,
    build_cluster_graphs,
    build_cluster_policies,
)

REPOS = [
    "https://github.com/mitodl/ol-infrastructure",
    "https://github.com/mitodl/agent-kit",
]


def policies_for(repos: list[str]) -> dict[str, dict[str, object]]:
    return build_cluster_policies(build_cluster_graphs(repos))


class TestCoverage:
    def test_every_graph_is_governed_by_exactly_one_bundle(self):
        """The invariant: no graph ungoverned, none claimed twice.

        A graph missing from every ``applies_to`` is default-deny with a
        confusing error; a graph in two bundles trips omnigraph's "one bundle
        per graph scope" selector at boot.
        """
        graphs = build_cluster_graphs(REPOS)
        policies = build_cluster_policies(graphs)

        governed = [
            graph
            for spec in policies.values()
            for graph in spec["applies_to"]
            if graph != "cluster"
        ]

        assert sorted(governed) == sorted(graphs)
        assert len(governed) == len(set(governed))

    def test_code_bundle_lists_every_code_graph(self):
        policies = policies_for(REPOS)
        assert policies["code_rules"]["applies_to"] == [
            "code-github-com-mitodl-agent-kit",
            "code-github-com-mitodl-ol-infrastructure",
        ]

    def test_memory_and_bridge_are_scoped_to_their_own_graph(self):
        """The memory bundle must never govern a code graph, or vice versa.

        They grant different actors entirely — witan-ci has no access to the
        work graph, and witan-users' blanket `change` has no business on the
        CI-owned code graphs.
        """
        policies = policies_for(REPOS)
        assert policies["memory_rules"]["applies_to"] == [COUNCIL_GRAPH_ID]
        assert policies["bridge_rules"]["applies_to"] == [BRIDGE_GRAPH_ID]

    def test_server_bundle_is_cluster_scoped(self):
        """`graph_list` binds to the server root, not to any graph."""
        assert policies_for(REPOS)["server_rules"]["applies_to"] == ["cluster"]


class TestEdgeCases:
    def test_no_managed_repos_omits_the_code_bundle(self):
        """An empty `applies_to` governs nothing but reads as though it did."""
        policies = policies_for([])
        assert "code_rules" not in policies
        assert set(policies) == {"memory_rules", "bridge_rules", "server_rules"}

    def test_code_graph_list_is_sorted(self):
        """Unstable ordering churns the config hash, restarting the server."""
        forward = policies_for(REPOS)["code_rules"]["applies_to"]
        reversed_ = policies_for(list(reversed(REPOS)))["code_rules"]["applies_to"]
        assert forward == reversed_

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("memory_rules", "./memory.policy.yaml"),
            ("code_rules", "./code-graph.policy.yaml"),
            ("bridge_rules", "./bridge.policy.yaml"),
            ("server_rules", "./server.policy.yaml"),
        ],
    )
    def test_bundle_paths_match_the_image_layout(self, name, expected):
        """Paths are relative to the cluster config dir the image bakes into.

        A rename on either side leaves the server unable to load the bundle,
        which is a boot failure rather than a silent one — but it is a boot
        failure in the data tier, so it is pinned on both sides.
        """
        assert policies_for(REPOS)[name]["file"] == expected
