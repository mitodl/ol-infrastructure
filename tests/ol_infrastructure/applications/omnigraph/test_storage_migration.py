"""Tests for the storage-format migration script's pure logic.

The two functions here decide *what* gets migrated and *where* it lands, and
both fail silently when they are wrong: a short graph list quietly leaves a
repo's graph behind at the old root, and a botched repoint quietly rebuilds
into a root nobody is serving — with `load` reporting success either way.
Everything else in the script is subprocess orchestration against a real
cluster, which is what the runbook rehearsal covers.
"""

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from ol_infrastructure.applications.omnigraph.cluster_config import (
    build_cluster_graphs,
    build_cluster_policies,
)

# Loaded by path: the script lives in `scripts/` so it can be mounted into a
# pod as a file, and that directory is deliberately not an importable package.
_SCRIPT = (
    Path(__file__).parents[4]
    / "src"
    / "ol_infrastructure"
    / "applications"
    / "omnigraph"
    / "scripts"
    / "migrate_storage_format.py"
)
_spec = importlib.util.spec_from_file_location("migrate_storage_format", _SCRIPT)
if _spec is None or _spec.loader is None:  # pragma: no cover - path is a constant
    LOAD_MSG = f"could not load the migration script from {_SCRIPT}"
    raise RuntimeError(LOAD_MSG)
migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate)

REPOS = [
    "https://github.com/mitodl/ol-infrastructure",
    "https://github.com/mitodl/mit-learn",
    "https://github.com/mitodl/agent-kit",
]


def _cluster_yaml(tmp_path: Path, storage: str = "s3://ol-data-witan-ci") -> Path:
    """Build a cluster.yaml with the same code that generates the real one."""
    graphs = build_cluster_graphs(REPOS)
    target = tmp_path / "cluster.yaml"
    target.write_text(
        yaml.dump(
            {
                "version": 1,
                "metadata": {"name": "mitodl-witan-ci"},
                "state": {"backend": "cluster"},
                "storage": storage,
                "graphs": graphs,
                "policies": build_cluster_policies(graphs),
            },
            sort_keys=False,
        )
    )
    return target


def _schema_dir(tmp_path: Path) -> Path:
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    for name in ("schema.pg", "code-schema.pg", "bridge-schema.pg"):
        (schemas / name).write_text("node X { slug: String @key }\n")
    return schemas


def test_graph_ids_match_what_the_cluster_declares(tmp_path: Path) -> None:
    """Enumerated from the config, never re-derived from `managed_repos` — the
    `code-<repo>` ids are derived values, and rebuilding that list by hand is
    how a repo gets left behind at the old root.
    """
    expected = sorted(build_cluster_graphs(REPOS))

    found = migrate.graph_ids_from_cluster_config(_cluster_yaml(tmp_path))

    assert sorted(found) == expected
    assert "council" in found
    assert "code-bridge" in found


def test_policy_names_are_not_mistaken_for_graphs(tmp_path: Path) -> None:
    """`policies:` sits directly below `graphs:` and its entries are indented
    identically. Reading them as graph ids would send the migration looking for
    stores named `memory_rules`.
    """
    found = migrate.graph_ids_from_cluster_config(_cluster_yaml(tmp_path))

    assert not [g for g in found if g.endswith("_rules")]


def test_repoint_rewrites_only_the_storage_line(tmp_path: Path) -> None:
    """The rebuilt config must declare the same graphs at the new root."""
    config = migrate.build_rebuild_config(
        _cluster_yaml(tmp_path),
        tmp_path / "rebuild",
        _schema_dir(tmp_path),
        "s3://ol-data-witan-ci/fmt6",
    )

    lines = config.read_text().splitlines()
    assert "storage: s3://ol-data-witan-ci/fmt6" in lines
    assert "storage: s3://ol-data-witan-ci" not in lines
    # The graph declarations must survive verbatim — same ids, same schemas.
    assert sorted(migrate.graph_ids_from_cluster_config(config)) == sorted(
        build_cluster_graphs(REPOS)
    )
    assert config.read_text().count("schema:") == len(build_cluster_graphs(REPOS))


def test_schemas_are_staged_beside_the_config(tmp_path: Path) -> None:
    """`cluster apply` resolves `schema:` relative to the config directory, so
    the rebuild fails at the point of no return if these are not there.
    """
    rebuild = tmp_path / "rebuild"

    migrate.build_rebuild_config(
        _cluster_yaml(tmp_path), rebuild, _schema_dir(tmp_path), "s3://b/fmt6"
    )

    assert (rebuild / "schema.pg").exists()
    assert (rebuild / "code-schema.pg").exists()
    assert (rebuild / "bridge-schema.pg").exists()


@pytest.mark.parametrize(
    "bad_root",
    [
        "",
        "s3://ol-data-witan-ci/fmt<N>",  # unsubstituted placeholder
        "s3://ol-data-witan-ci",  # the root it is migrating away from
        "s3://ol-data-witan-ci/scratch",  # not a format-versioned prefix
        "/ol-data-witan-ci/fmt6",  # not an S3 URI
    ],
)
def test_a_root_that_is_not_fmt_n_is_refused(tmp_path: Path, bad_root: str) -> None:
    """The empty case is the one that matters most. `cluster validate` accepts a
    blank `storage:`, and the repoint check compares against
    `f"storage: {new_root}"` — which for an empty root is the very line it
    would be rejecting, so it would pass its own guard. `<` and `>` are legal
    in S3 keys, so an unsubstituted `fmt<N>` becomes a real prefix.
    """
    with pytest.raises(SystemExit):
        migrate.build_rebuild_config(
            _cluster_yaml(tmp_path),
            tmp_path / "rebuild",
            _schema_dir(tmp_path),
            bad_root,
        )


def test_a_config_with_no_storage_line_is_refused(tmp_path: Path) -> None:
    """A renamed or moved `storage:` key means the repoint silently did
    nothing, and the rebuild would land at whatever the config already said.
    """
    source = _cluster_yaml(tmp_path)
    source.write_text(
        "\n".join(
            line
            for line in source.read_text().splitlines()
            if not line.startswith("storage:")
        )
    )

    with pytest.raises(SystemExit, match="was not repointed"):
        migrate.build_rebuild_config(
            source, tmp_path / "rebuild", _schema_dir(tmp_path), "s3://b/fmt6"
        )


def test_row_counts_are_parsed_per_table() -> None:
    """Verification compares per table. A total-row check would pass a rebuild
    that put every row in the wrong table.
    """
    snapshot = (
        "branch: main\n"
        "manifest_version: 4\n"
        "internal_schema_version: 6\n"
        "edge:Supersedes v1 branch=main rows=0\n"
        "node:Memory v2 branch=main rows=41\n"
        "node:Task v1 branch=main rows=7\n"
    )

    counts = {
        m["table"]: int(m["rows"]) for m in migrate.SNAPSHOT_ROW_RE.finditer(snapshot)
    }

    assert counts == {"edge:Supersedes": 0, "node:Memory": 41, "node:Task": 7}
    assert migrate.SNAPSHOT_SCHEMA_RE.search(snapshot).group(1) == "6"


def _export(tmp_path: Path, nodes: int, edges: int = 0) -> Path:
    """Build an export shaped like omnigraph's: node records, then edge records."""
    path = tmp_path / "graph.jsonl"
    lines = [
        json.dumps({"type": "Memory", "data": {"slug": f"m-{i}"}}) for i in range(nodes)
    ]
    lines += [
        json.dumps({"edge": "RelatesTo", "from": f"m-{i}", "to": f"m-{i + 1}"})
        for i in range(edges)
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_a_small_export_is_loaded_whole(tmp_path: Path) -> None:
    """The common case stays one load, with no temporary files."""
    export = _export(tmp_path, nodes=10)

    assert migrate.chunk_export(export) == [export]


def test_an_export_over_the_row_cap_is_split(tmp_path: Path) -> None:
    """Omnigraph 0.9 caps a keyed write at 8,192 rows per table, so one `load`
    of a populated graph fails outright.
    """
    export = _export(tmp_path, nodes=migrate.KEYED_ROW_CAP + 100)

    batches = migrate.chunk_export(export)

    assert len(batches) > 1
    for batch in batches:
        rows = [ln for ln in batch.read_text().splitlines() if ln.strip()]
        assert len(rows) <= migrate.KEYED_ROW_CAP
    total = sum(
        len([ln for ln in b.read_text().splitlines() if ln.strip()]) for b in batches
    )
    assert total == migrate.KEYED_ROW_CAP + 100


def test_every_node_precedes_every_edge_across_batches(tmp_path: Path) -> None:
    """An edge resolves against nodes already persisted or present in the same
    batch; an endpoint in neither fails the whole load with `dst '...' not
    found`. That ordering outranks the row bound.
    """
    export = _export(tmp_path, nodes=migrate.KEYED_ROW_CAP + 10, edges=50)

    seen_edge = False
    for batch in migrate.chunk_export(export):
        for line in batch.read_text().splitlines():
            if not line.strip():
                continue
            if "edge" in json.loads(line):
                seen_edge = True
            else:
                assert not seen_edge, "a node followed an edge across batches"
    assert seen_edge


def test_a_format_that_did_not_move_is_reported() -> None:
    """Both images on one format means the outage bought nothing — or the wrong
    image was named as migrate_from_image.
    """
    problems = migrate.check_format_moved({"council": 4}, {"council": 4})

    assert problems
    assert "did not move" in problems[0]


def test_graphs_left_on_different_formats_are_reported() -> None:
    """Cutting over onto a cluster the new binary can only partly open."""
    problems = migrate.check_format_moved(
        {"council": 4, "code-bridge": 4}, {"council": 6, "code-bridge": 4}
    )

    assert problems
    assert "not all on one format" in problems[0]


def test_a_clean_format_move_reports_nothing() -> None:
    """Every graph moved to one new format — the shape a cutover needs."""
    assert not migrate.check_format_moved(
        {"council": 4, "code-bridge": 4}, {"council": 6, "code-bridge": 6}
    )


def test_bucket_is_extracted_from_either_root_shape() -> None:
    """The source root is the bare bucket only before the first cutover; after
    one it carries a prefix. Both must resolve to the same bucket, which is what
    the migration checks instead of containment.
    """
    assert migrate.bucket_of("s3://ol-data-witan-ci") == "ol-data-witan-ci"
    assert migrate.bucket_of("s3://ol-data-witan-ci/fmt5") == "ol-data-witan-ci"
