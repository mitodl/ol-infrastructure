"""Cluster catalog shape: the graph ids, schema/policy filenames, and the
``graphs:`` / ``policies:`` blocks of the generated ``cluster.yaml``.

Split out of ``data_tier.py`` so it can be imported without pulling in
``pulumi_aws`` and the EKS/S3 components, which need AWS credentials and a
region at import time. That makes this module unit-testable in CI, which
matters more here than it looks: ``code_graph_id`` is a byte-for-byte shared
contract with agent-kit's client-side ``witan_code.config.graph_id``, and
``build_cluster_policies`` decides which Cedar bundle governs which graph — a
graph that ends up in no bundle is not "open", it is default-deny, and it
fails every write with an error pointing at policy rather than at the missing
wiring. ``storage.py`` exists for the same reason.
"""

import hashlib
import re

# ── Cluster graph ids ────────────────────────────────────────────────────────
#
# Graph ids track the owning published package rather than the omnigraph
# default branch name: omnigraph's default *branch* is `main`, so a graph also
# called `main` conflates the two. `council` is what witan-council asks for
# (WITAN_MEMORY_GRAPH, default `council` in agent-kit
# mcp/servers/witan/witan/config.py) — a cluster that declares anything else
# serves a graph no client addresses.
COUNCIL_GRAPH_ID = "council"
# Layer 2.5 cross-repo bridge, the deployed analogue of witan-code's local
# `_bridge.omni` store. Fixed id, not derived from any repo.
BRIDGE_GRAPH_ID = "code-bridge"
CODE_GRAPH_PREFIX = "code-"

# Schema filenames, all three baked into the omnigraph-server image at
# CLUSTER_CONFIG_DIR by the image build (agent-kit
# docker/omnigraph-server.Dockerfile). Not sourced here: this Pulumi program
# has no access to agent-kit's working tree at apply time.
COUNCIL_SCHEMA_FILE = "schema.pg"
CODE_SCHEMA_FILE = "code-schema.pg"
BRIDGE_SCHEMA_FILE = "bridge-schema.pg"

# Cedar bundle filenames, baked into the image next to the schemas by the same
# build and for the same reason. Their committed `groups:` are fixtures — the
# image entrypoint rewrites membership from the mounted actor-token map before
# `cluster apply`, because `witan-users` has to track the hourly token-sync
# job's output and this program cannot see it. See agent-kit
# mcp/servers/witan/policy/README.md § "Group membership is rendered at boot".
MEMORY_POLICY_FILE = "memory.policy.yaml"
CODE_POLICY_FILE = "code-graph.policy.yaml"
BRIDGE_POLICY_FILE = "bridge.policy.yaml"
SERVER_POLICY_FILE = "server.policy.yaml"

_GRAPH_ID_MAX_LEN = 64


def code_graph_id(repo: str) -> str:
    """Canonical cluster graph id for ``repo``'s code graph.

    e.g. ``https://github.com/mitodl/ol-django`` ->
    ``code-github-com-mitodl-ol-django``.

    SHARED CONTRACT — this is a mirror of ``witan_code.config.graph_id``
    (agent-kit, mcp/servers/witan-code/witan_code/config.py), which is what
    selects the ``--graph`` id on the *client* side. This function declares the
    graph the cluster creates. They MUST agree byte-for-byte or a client will
    address a graph that was never created. Any change has to land in lockstep
    on both sides — see agent-kit task
    tk-code-graph-deployment-topology-shared-per-repo-c-cac400.

    Strips the URI scheme, collapses every run of non-alphanumerics to ``-``,
    lowercases, and prefixes ``code-``. omnigraph graph ids must match
    ``^[a-zA-Z0-9-]{1,64}$`` (note: no underscores), so ids that would exceed
    64 characters are truncated and disambiguated with a hash of the full repo
    URI, keeping distinct long repos from colliding.
    """
    body = re.sub(r"(?i)^[a-z][a-z0-9+.-]*://", "", repo)  # strip scheme
    body = re.sub(r"[^a-zA-Z0-9]+", "-", body).strip("-").lower()
    candidate = f"{CODE_GRAPH_PREFIX}{body}"
    if len(candidate) <= _GRAPH_ID_MAX_LEN:
        return candidate
    digest = hashlib.sha256(repo.encode()).hexdigest()[:8]
    keep = _GRAPH_ID_MAX_LEN - len(CODE_GRAPH_PREFIX) - len(digest) - 1
    return f"{CODE_GRAPH_PREFIX}{body[:keep].strip('-')}-{digest}"


def build_cluster_graphs(managed_repos: list[str]) -> dict[str, dict[str, str]]:
    """Build the ``graphs:`` block of cluster.yaml for ``managed_repos``.

    One graph per repo rather than one shared code graph (ADR-0009 follow-up,
    decided: option A), matching the per-repo model witan-code already uses
    locally. Each is a self-contained Lance store under
    ``s3://<bucket>/graphs/<graph-id>.omni/``, so per-repo isolation costs
    nothing beyond the entry here, and per-user/per-session WIP branches live
    inside their own repo's graph.

    Adding a repo is deliberately a config change rather than ad-hoc
    self-creation by a client: an unmanaged repo should fail to resolve rather
    than silently mint a graph nobody provisioned or backs up. The
    ``cluster apply`` and server restart it requires are both automatic — the
    converge Job and the Deployment's pod template share one config hash, so
    editing this list creates the graph and restarts the server into it in the
    same deploy.

    Raises ``ValueError`` on a duplicate derived id — two repo URIs that
    normalize to the same graph id would otherwise silently share one store.
    """
    graphs: dict[str, dict[str, str]] = {
        COUNCIL_GRAPH_ID: {"schema": COUNCIL_SCHEMA_FILE},
        BRIDGE_GRAPH_ID: {"schema": BRIDGE_SCHEMA_FILE},
    }
    for repo in managed_repos:
        graph = code_graph_id(repo)
        if graph in graphs:
            msg = (
                f"Duplicate cluster graph id {graph!r} derived from repo "
                f"{repo!r}. Two managed repos normalize to the same id, which "
                "would silently share one store."
            )
            raise ValueError(msg)
        graphs[graph] = {"schema": CODE_SCHEMA_FILE}
    return graphs


def build_cluster_policies(
    cluster_graphs: dict[str, dict[str, str]],
) -> dict[str, dict[str, object]]:
    """Build the ``policies:`` block of cluster.yaml for ``cluster_graphs``.

    Wires the four Cedar bundles baked into the image onto the graphs they
    govern. Derived from the graph map rather than rebuilt from
    ``managed_repos`` so a graph can never exist without a bundle: an
    ungoverned graph is not "open", it is default-deny, and it would fail every
    write with an error pointing at policy rather than at the missing wiring.

    APPLYING ANY BUNDLE IS A HARD CUTOVER TO AUTHENTICATED-EVERYTHING. With a
    ``policies:`` block the server refuses to boot without bearer tokens
    ("policy file is configured but no bearer tokens — every request would 401
    because no token can ever match"), and once booted an actor with no grant
    gets ``policy denied action '…' for unknown actor '…'``. Unconditional
    rather than gated per environment because every environment finished its
    actor-token rollout on 2026-08-05 and nothing is using the graphs yet —
    ``__main__.py`` asserts token sync is enabled, which is the precondition
    that actually matters.

    The server bundle is scoped to ``cluster`` rather than to a graph:
    ``graph_list`` binds to ``Omnigraph::Server::"root"``. It is deploy-time
    only — omnigraph 0.8.1's offline ``policy validate``/``policy test`` load
    every bundle under the per-graph engine and reject a server-scoped action,
    so agent-kit's CI fixture deliberately omits it and it is validated here by
    omnigraph-server at boot instead.
    """
    code_graph_ids = sorted(
        graph
        for graph, spec in cluster_graphs.items()
        if spec["schema"] == CODE_SCHEMA_FILE
    )
    policies: dict[str, dict[str, object]] = {
        "memory_rules": {
            "file": f"./{MEMORY_POLICY_FILE}",
            "applies_to": [COUNCIL_GRAPH_ID],
        },
        "bridge_rules": {
            "file": f"./{BRIDGE_POLICY_FILE}",
            "applies_to": [BRIDGE_GRAPH_ID],
        },
        "server_rules": {
            "file": f"./{SERVER_POLICY_FILE}",
            "applies_to": ["cluster"],
        },
    }
    # Omitted entirely when no repo is managed: a bundle with an empty
    # `applies_to` governs nothing, and declaring one reads as though the code
    # graphs were covered when there are none.
    if code_graph_ids:
        policies["code_rules"] = {
            "file": f"./{CODE_POLICY_FILE}",
            "applies_to": code_graph_ids,
        }
    return policies
