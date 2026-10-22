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


def build_cluster_graphs(
    managed_repos: list[str], withheld_graphs: list[str] | None = None
) -> dict[str, dict[str, str]]:
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

    ``withheld_graphs`` (``omnigraph:withheld_graphs``) leaves graphs out of
    the block that would otherwise be declared. It exists to rebuild a graph
    whose schema change omnigraph cannot migrate in place (e.g. dropping a
    column): there is no replace operation in ``cluster apply``, and its plan
    is keyed by graph id, so one ``cluster.yaml`` cannot both delete and
    create the same graph. The rebuild is one deploy with the graph withheld
    (and in ``omnigraph:graph_removal_allowlist``), whose apply deletes it,
    then one without, whose apply creates it empty from the image's schema.

    Raises ``ValueError`` on a duplicate derived id — two repo URIs that
    normalize to the same graph id would otherwise silently share one store —
    and on a withheld entry that is not a list of graph ids, names no graph
    this would declare, or names ``council``.
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
    withheld = _validate_withheld_graphs(
        [] if withheld_graphs is None else withheld_graphs, list(graphs)
    )
    return {graph: spec for graph, spec in graphs.items() if graph not in withheld}


def _validate_withheld_graphs(
    withheld_graphs: list[str], declarable_graph_ids: list[str]
) -> list[str]:
    """Return ``withheld_graphs``, refusing an entry that cannot be rebuilt.

    :param withheld_graphs: graph ids from ``omnigraph:withheld_graphs``.
    :param declarable_graph_ids: the ids cluster.yaml would declare without it.
    :raises ValueError: when the value is not a list of graph ids, an entry
        names no declarable graph, or an entry is ``council``.
    """
    require_graph_id_list(withheld_graphs, "withheld_graphs")
    unknown = sorted(set(withheld_graphs) - set(declarable_graph_ids))
    if unknown:
        # A typo would otherwise withhold nothing, and the deploy that was
        # meant to delete a graph would succeed without touching it.
        msg = (
            f"omnigraph:withheld_graphs names {unknown}, which cluster.yaml "
            "would not declare anyway."
        )
        raise ValueError(msg)
    if COUNCIL_GRAPH_ID in withheld_graphs:
        # The code graphs and the bridge are derived from checkouts, so a
        # reindex puts their contents back. council holds what people and
        # agents wrote, and nothing re-derives it.
        msg = (
            f"omnigraph:withheld_graphs names {COUNCIL_GRAPH_ID!r}. Withholding "
            "a graph deletes its storage and history, and council's contents "
            "are not derived from anything a reindex can replay."
        )
        raise ValueError(msg)
    return withheld_graphs


def require_graph_id_list(value: object, key: str) -> list[str]:
    """Return ``value``, refusing anything but a list of graph ids.

    ``Config.get_object`` JSON-decodes the stack value, so a JSON string such
    as ``"code-bridge"`` arrives as a ``str``, which would otherwise be read as
    a list of its characters.

    :param value: the decoded ``omnigraph:<key>`` value.
    :param key: the config key, for the error.
    :raises ValueError: when ``value`` is not a list of strings.
    """
    if not isinstance(value, list) or not all(
        isinstance(graph, str) for graph in value
    ):
        msg = f"omnigraph:{key} must be a list of graph ids, got {value!r}"
        raise ValueError(msg)
    return value


def require_withheld_graphs_removable(
    withheld_graphs: list[str], removable_graphs: list[str]
) -> None:
    """Refuse a withheld graph whose deletion is not allow-listed.

    Withholding always means deleting. Without the allow-list entry the
    ConfigMap is updated and the plan gate then fails the deploy, but a
    failed deploy does not roll the ConfigMap back, so the server's next boot
    (e.g. the nightly window scaling it back up) refuses to start.

    :raises ValueError: naming the withheld graphs that are not allow-listed.
    """
    missing = sorted(set(withheld_graphs) - set(removable_graphs))
    if missing:
        msg = (
            f"omnigraph:withheld_graphs names {missing}, which "
            "omnigraph:graph_removal_allowlist does not. Withholding a graph "
            "deletes it, so list it in both."
        )
        raise ValueError(msg)


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
    # A bundle is omitted with its graph (withheld_graphs). omnigraph refuses
    # an `applies_to` naming an undeclared graph (`dangling_graph_reference`,
    # v0.13.0 omnigraph-cluster config.rs), so keeping it would fail the plan
    # of the deploy that removes the graph.
    policies: dict[str, dict[str, object]] = {}
    if COUNCIL_GRAPH_ID in cluster_graphs:
        policies["memory_rules"] = {
            "file": f"./{MEMORY_POLICY_FILE}",
            "applies_to": [COUNCIL_GRAPH_ID],
        }
    if BRIDGE_GRAPH_ID in cluster_graphs:
        policies["bridge_rules"] = {
            "file": f"./{BRIDGE_POLICY_FILE}",
            "applies_to": [BRIDGE_GRAPH_ID],
        }
    policies["server_rules"] = {
        "file": f"./{SERVER_POLICY_FILE}",
        "applies_to": ["cluster"],
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
