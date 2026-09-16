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
from typing import Any

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

    The shape is verbatim `snapshot` output from omnigraph 0.10.0, and 0.11.0
    prints the same per-table lines. The 0.8 `rows=N` shape this regex used to
    expect matched nothing on either, so the Job stopped at its first graph.
    """
    snapshot = (
        "graph_branch: main\n"
        "graph_manifest_version: 5\n"
        "internal_schema_version: 6\n"
        "edge type 'Supersedes' published_dataset_version=1 "
        "native_dataset_branch=main entities=0\n"
        "node type 'Memory' published_dataset_version=2 "
        "native_dataset_branch=main entities=41\n"
        "node type 'Task' published_dataset_version=1 "
        "native_dataset_branch=main entities=7\n"
    )

    counts = {
        m["table"]: int(m["rows"]) for m in migrate.SNAPSHOT_ROW_RE.finditer(snapshot)
    }

    assert counts == {"Supersedes": 0, "Memory": 41, "Task": 7}
    assert migrate.SNAPSHOT_SCHEMA_RE.search(snapshot).group(1) == "6"


_KEYED_SCHEMA = """\
node Memory { slug: String @key }
node Topic { slug: String @key }

// edge Commented: Memory -> Memory { @key(@src, @dst) }
edge Tagged: Memory -> Topic {
    @key(@src, @dst)
    confidence: enum(asserted, inferred)? @index
    created_at: DateTime?
}
edge RelatedTo: Memory -> Memory { @key(@dst, @src) }
edge Loose: Memory -> Memory {
    role: String?
}
edge Bare: Memory -> Memory
"""


def test_keyed_edge_types_are_read_from_the_schema(tmp_path: Path) -> None:
    """The export cannot say which edge types are keyed; only the schema the
    rebuild creates can, and a commented-out declaration must not count.
    """
    schema = tmp_path / "schema.pg"
    schema.write_text(_KEYED_SCHEMA)

    assert migrate.keyed_edge_types(schema) == {"Tagged", "RelatedTo"}


def test_a_key_beyond_the_endpoint_pair_is_refused(tmp_path: Path) -> None:
    """Collapsing on (from, to) would merge rows a wider key keeps apart."""
    schema = tmp_path / "schema.pg"
    schema.write_text(
        "node M { slug: String @key }\n"
        "edge Wide: M -> M {\n    @key(@src, @dst, kind)\n    kind: String\n}\n"
    )

    with pytest.raises(SystemExit, match="Wide"):
        migrate.keyed_edge_types(schema)


def test_schema_files_are_mapped_per_graph(tmp_path: Path) -> None:
    """Each graph's keyed types come from its own schema, not council's."""
    mapping = migrate.schema_files_by_graph(_cluster_yaml(tmp_path))

    assert set(mapping) == set(build_cluster_graphs(REPOS))
    assert mapping["council"] == "schema.pg"
    assert mapping["code-bridge"] == "bridge-schema.pg"


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> Path:
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _tagged(row_id: str, **data: object) -> dict[str, object]:
    return {"edge": "Tagged", "from": "m1", "to": "t1", "data": {"id": row_id, **data}}


def test_normalize_relocates_node_ids_and_drops_keyed_edge_ids(tmp_path: Path) -> None:
    """0.11 refuses `data.id` on load, and refuses a 0.10 ULID on a keyed edge
    because it does not match the id the key derives. Unkeyed edges keep theirs.
    """
    export = _write_jsonl(
        tmp_path / "g.jsonl",
        [
            {"type": "Memory", "data": {"id": "m1", "slug": "m1"}},
            _tagged("01A", confidence="inferred"),
            {"edge": "Loose", "from": "m1", "to": "m2", "data": {"id": "01B"}},
        ],
    )

    normalized, collapsed = migrate.normalize_export(export, {"Tagged"})

    rows = _read_jsonl(normalized)
    memory = next(r for r in rows if r.get("type") == "Memory")
    assert memory["id"] == "m1"
    assert "id" not in memory["data"]
    tagged = next(r for r in rows if r.get("edge") == "Tagged")
    assert "id" not in tagged
    assert "id" not in tagged["data"]
    loose = next(r for r in rows if r.get("edge") == "Loose")
    assert loose["id"] == "01B"
    assert collapsed == {}


def test_an_asserted_duplicate_outranks_a_newer_inferred_one(tmp_path: Path) -> None:
    """Two rows for one keyed pair fail the whole load. The survivor is a whole
    row, and a link someone named beats one witan derived, however recent.
    """
    export = _write_jsonl(
        tmp_path / "g.jsonl",
        [
            _tagged(
                "01A", confidence="asserted", role="named", created_at=1767225600000
            ),
            _tagged(
                "01B",
                confidence="inferred",
                role="derived",
                created_at="2026-06-01T00:00:00",
            ),
        ],
    )

    normalized, collapsed = migrate.normalize_export(export, {"Tagged"})

    assert [r["data"]["role"] for r in _read_jsonl(normalized)] == ["named"]
    assert collapsed == {"Tagged": 1}


def test_the_newest_created_at_wins_across_timestamp_shapes(tmp_path: Path) -> None:
    """0.10 exports DateTime as epoch milliseconds, 0.11 as a naive ISO string,
    and edges written before 2026-09 have none, which ranks oldest.
    """
    export = _write_jsonl(
        tmp_path / "g.jsonl",
        [
            _tagged("01A", role="unstamped", created_at=None),
            _tagged("01B", role="january", created_at=1767225600000),
            _tagged("01C", role="march", created_at="2026-03-01T00:00:00"),
            _tagged("01D", role="february", created_at=1769904000000),
        ],
    )

    normalized, collapsed = migrate.normalize_export(export, {"Tagged"})

    assert [r["data"]["role"] for r in _read_jsonl(normalized)] == ["march"]
    assert collapsed == {"Tagged": 3}


def test_an_exact_tie_keeps_the_later_row(tmp_path: Path) -> None:
    """Identical rank is the common 0.10 duplicate: the same unstamped edge
    appended by every re-link. The later row in the export survives, which is
    deterministic for a given export but is not necessarily the last write.
    """
    export = _write_jsonl(
        tmp_path / "g.jsonl",
        [_tagged("01A", role="a"), _tagged("01B", role="b"), _tagged("01C", role="c")],
    )

    normalized, _ = migrate.normalize_export(export, {"Tagged"})

    assert [r["data"]["role"] for r in _read_jsonl(normalized)] == ["c"]


def test_a_tie_holds_across_a_long_gap_between_duplicates(tmp_path: Path) -> None:
    """Duplicates sit anywhere in the export, which is why the collapse takes
    two passes over the file rather than one pass buffering rewritten rows: the
    rank is decided in pass 1 and the survivor streamed out in pass 2. Rank ties
    still keep the later row, and the rows in between still pass through.
    """
    filler: list[dict[str, object]] = [
        {"type": "Memory", "data": {"id": f"m{n}", "slug": f"m{n}"}} for n in range(300)
    ]
    export = _write_jsonl(
        tmp_path / "g.jsonl",
        [
            _tagged("01A", role="first"),
            *filler,
            _tagged("01B", role="middle"),
            *filler,
            _tagged("01C", role="last"),
        ],
    )

    normalized, collapsed = migrate.normalize_export(export, {"Tagged"})

    rows = _read_jsonl(normalized)
    assert [r["data"]["role"] for r in rows if r.get("edge") == "Tagged"] == ["last"]
    assert len([r for r in rows if r.get("type") == "Memory"]) == 600
    assert collapsed == {"Tagged": 2}


def test_indexes_are_built_on_the_rebuilt_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`load` builds no indexes in 0.11, so optimize has to run against the new
    store; anything it defers is surfaced rather than dropped.
    """
    calls: list[list[str]] = []
    optimize_out = {
        "datasets": [
            {"type_key": "edge:WorksOn", "pending_indexes": []},
            {
                "type_key": "node:Memory",
                "pending_indexes": [
                    {
                        "type_key": "node:Memory",
                        "property": "embedding",
                        "reason": "property has no non-null vectors to train on yet",
                    }
                ],
            },
        ]
    }

    def fake_run(argv: list[str], **_: Any) -> Any:
        calls.append(argv)
        return migrate.subprocess.CompletedProcess(argv, 0, json.dumps(optimize_out))

    monkeypatch.setattr(migrate, "run", fake_run)

    pending = migrate.build_indexes("omnigraph", "s3://b/fmt9/graphs/council.omni")

    assert calls == [
        [
            "omnigraph",
            "optimize",
            "--store",
            "s3://b/fmt9/graphs/council.omni",
            "--json",
        ]
    ]
    assert [p["property"] for p in pending] == ["embedding"]


def test_verify_expects_the_collapsed_edge_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 0.10 baseline counts duplicates the rebuild deliberately removed, so
    strict equality with it would fail every graph that had any.
    """
    monkeypatch.setattr(
        migrate, "snapshot_tables", lambda *_: {"Memory": 2, "Tagged": 1}
    )

    report, mismatched = migrate.verify(
        "omnigraph",
        "file:///new",
        ["council"],
        {"council": {"Memory": 2, "Tagged": 3}},
        {"council": {"Tagged": 2}},
    )

    assert mismatched == []
    assert report["council"]["expected"] == {"Memory": 2, "Tagged": 1}
    assert report["council"]["collapsed_duplicates"] == {"Tagged": 2}


def test_verify_still_fails_a_short_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse accounting must not excuse a table that lost rows it kept."""
    monkeypatch.setattr(
        migrate, "snapshot_tables", lambda *_: {"Memory": 1, "Tagged": 1}
    )

    report, mismatched = migrate.verify(
        "omnigraph",
        "file:///new",
        ["council"],
        {"council": {"Memory": 2, "Tagged": 3}},
        {"council": {"Tagged": 2}},
    )

    assert mismatched == ["council"]
    assert report["council"]["changed_tables"] == ["Memory"]


def test_verify_accepts_an_empty_table_the_new_schema_added(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A graph created before its schema grew a type has no baseline row for
    that table, and the rebuild creates it empty. Found on a real local store
    that predates TaskComment.
    """
    monkeypatch.setattr(
        migrate, "snapshot_tables", lambda *_: {"Memory": 2, "TaskComment": 0}
    )

    report, mismatched = migrate.verify(
        "omnigraph", "file:///new", ["council"], {"council": {"Memory": 2}}, {}
    )

    assert mismatched == []
    assert report["council"]["new_tables"] == ["TaskComment"]


def test_verify_fails_a_new_table_that_has_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No export can put rows in a table the old graph lacked, so rows there
    mean the rebuild loaded something the baseline never counted.
    """
    monkeypatch.setattr(
        migrate, "snapshot_tables", lambda *_: {"Memory": 2, "TaskComment": 3}
    )

    report, mismatched = migrate.verify(
        "omnigraph", "file:///new", ["council"], {"council": {"Memory": 2}}, {}
    )

    assert mismatched == ["council"]
    assert report["council"]["changed_tables"] == ["TaskComment"]


def test_cutover_instructions_set_both_paired_config_values() -> None:
    """omnigraph:internal_schema_version is required alongside storage_prefix
    (ol-infrastructure's storage.py::validate_internal_schema_version) — the
    printed cutover command has to set both, or the very next `pulumi
    preview` following it fails.
    """
    instructions = migrate.cutover_instructions("fmt6", 6)
    assert "pulumi config set omnigraph:storage_prefix fmt6 " in instructions
    assert "pulumi config set omnigraph:internal_schema_version 6 " in instructions


def test_cutover_instructions_clear_the_migration_knobs() -> None:
    """Clearing migrate_from_image/migrate_to_prefix in the SAME config
    change as the cutover is what __main__.py's own
    `if MIGRATE_TO_PREFIX == STORAGE_PREFIX` guard requires — its error
    message says so, and this is that advice followed.
    """
    instructions = migrate.cutover_instructions("fmt6", 6)
    assert "pulumi config rm omnigraph:migrate_from_image " in instructions
    assert "pulumi config rm omnigraph:migrate_to_prefix " in instructions


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


def test_batch_numbering_continues_across_the_node_and_edge_groups(
    tmp_path: Path,
) -> None:
    """The two groups are written by separate passes, so their numbering has to
    share a counter: restarting it for the edges would have edge batches
    reopening the node batch files and silently dropping every node row.
    """
    export = _export(
        tmp_path, nodes=migrate.KEYED_ROW_CAP + 10, edges=migrate.KEYED_ROW_CAP + 10
    )

    batches = migrate.chunk_export(export)

    assert len(batches) == len({b.name for b in batches}), "a batch file was reused"
    total = sum(
        len([ln for ln in b.read_text().splitlines() if ln.strip()]) for b in batches
    )
    assert total == 2 * (migrate.KEYED_ROW_CAP + 10)


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


def test_policy_bundles_are_staged_alongside_the_schemas(tmp_path: Path) -> None:
    """The generated cluster.yaml's `policies:` block references
    `./memory.policy.yaml` and friends, which sit in the same baked directory
    as the schemas. Staging only `*.pg` left those unresolvable and
    `omnigraph cluster validate` refused the rebuilt config — after a clean
    16-graph export, at the point of no return, on the first real run.
    """
    schemas = _schema_dir(tmp_path)
    for policy in (
        "memory.policy.yaml",
        "code-graph.policy.yaml",
        "bridge.policy.yaml",
        "server.policy.yaml",
    ):
        (schemas / policy).write_text("version: 1\nrules: []\n")
    rebuild = tmp_path / "rebuild"

    migrate.build_rebuild_config(
        _cluster_yaml(tmp_path), rebuild, schemas, "s3://b/fmt6"
    )

    staged = {p.name for p in rebuild.iterdir()}
    assert {"memory.policy.yaml", "code-graph.policy.yaml"} <= staged
    assert {"schema.pg", "code-schema.pg", "bridge-schema.pg"} <= staged


def test_the_source_cluster_yaml_never_shadows_the_repointed_one(
    tmp_path: Path,
) -> None:
    """The staging copy must not bring the original cluster.yaml across — it
    would overwrite the repointed one and send the rebuild at the OLD root.
    """
    schemas = _schema_dir(tmp_path)
    (schemas / "cluster.yaml").write_text("storage: s3://ol-data-witan-ci\n")
    rebuild = tmp_path / "rebuild"

    config = migrate.build_rebuild_config(
        _cluster_yaml(tmp_path), rebuild, schemas, "s3://b/fmt6"
    )

    assert "storage: s3://b/fmt6" in config.read_text().splitlines()
    assert "storage: s3://ol-data-witan-ci" not in config.read_text().splitlines()
