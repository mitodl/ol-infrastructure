"""Rebuild every cluster graph at a new storage root, in-cluster, in one pod.

omnigraph storage is strict-single-version: a binary reads exactly one
internal-schema, there is no in-place migration, and the gate is enforced in
both directions including read-only opens. So a release that bumps the format
cannot be rolled out — every graph has to be exported by the OLD binary and
reloaded by the NEW one, at a DIFFERENT root, with the old root left
byte-for-byte intact as the rollback.

``docs/omnigraph-storage-format-upgrade-runbook.md`` is the procedure this
automates, and it stays the reference for everything around it — pausing the
Concourse pipeline, scaling the Deployment down, the cutover, retiring the old
root. This script is steps 2, 3, 4 and 6 of that runbook: baseline, export,
rebuild, verify.

WHY IN ONE POD. The runbook runs two ``kubectl run`` pods and moves every
export through the operator's workstation with ``kubectl exec ... > file``,
because a hand-started pod carries only one image. That transfer is the slowest
part of the migration and the only step that can fail *silently*: a truncated
``kubectl exec`` redirect yields a short JSONL file, and ``load`` then reports
success over the top of it. Here an initContainer on the OLD image copies its
binary into a shared volume, so one pod holds both binaries and every byte
moves S3 -> pod -> S3 without a laptop in the path.

WHAT IT DELIBERATELY DOES NOT DO. It never repoints the live cluster. That is
``omnigraph:storage_prefix``, a Pulumi config value, and this pod has no
business holding Pulumi credentials; patching the ConfigMap instead would make
it the second writer of a path Pulumi owns, which is the failure mode
``sync_actor_tokens.py`` exists to avoid on the token map. The script verifies
and stops, leaving a machine-readable verdict for whatever drives the cutover.

The exports stay on the pod's disk for the life of the Job, so a failed
verification can be inspected before anything is repointed. Nothing here writes
to the old root.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# `s3://<bucket>/fmt<N>`, where N is the NEW internal-schema number. Anchored
# and digit-only on purpose: `<` and `>` are legal in S3 object keys, so an
# unsubstituted `fmt<N>` copied out of the runbook would otherwise become a
# real prefix, and the rebuild would land somewhere nobody is looking with
# `load` reporting success on top of it.
NEW_ROOT_RE = re.compile(r"^s3://[a-z0-9.-]+/fmt[0-9]+$")

# `rows=` counts out of `omnigraph snapshot`, e.g.
# `node:Memory v2 branch=main rows=41`. Compared PER TABLE between old and new:
# a total-row check would pass a rebuild that put every row in the wrong table,
# and a whole-store check would pass one that silently dropped an empty one.
SNAPSHOT_ROW_RE = re.compile(r"^(?P<table>\S+)\s+.*\brows=(?P<rows>\d+)", re.MULTILINE)
SNAPSHOT_SCHEMA_RE = re.compile(r"^internal_schema_version:\s*(\d+)", re.MULTILINE)

VERDICT_PATH = Path("/tmp/migration-verdict.json")  # noqa: S108

LOG = logging.getLogger("migrate-storage-format")

# One graph's verification record: an `ok` flag, the before/after per-table
# counts, and the two difference lists. Heterogeneous by nature, so the
# values are `object` rather than a lie about them all being one type.
GraphReport = dict[str, object]


def env(name: str, default: str | None = None) -> str:
    """Read a required environment variable, or exit naming it."""
    value = os.environ.get(name, default)
    if not value:
        sys.exit(f"{name} is required")
    return value


def run(
    argv: list[str], *, stdout_path: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run an omnigraph command, streaming a large stdout straight to disk.

    ``stdout_path`` matters for ``export``: a repo-scale graph's JSONL is not
    something to hold in memory and write out again, and the point of this
    script is that the bytes never take a detour. That branch runs in binary
    mode (no ``text=True``) because the bytes go to a file untouched, so the
    two branches are decoded separately and only the normalised result is
    returned.
    """
    printable = " ".join(argv)
    LOG.info("    $ %s", printable)
    if stdout_path is not None:
        with stdout_path.open("wb") as fh:
            streamed = subprocess.run(  # noqa: S603
                argv, stdout=fh, stderr=subprocess.PIPE, check=False
            )
        returncode = streamed.returncode
        stderr, stdout = streamed.stderr.decode(errors="replace"), ""
    else:
        captured = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, check=False
        )
        returncode = captured.returncode
        stderr, stdout = captured.stderr, captured.stdout
    if check and returncode != 0:
        sys.exit(f"!!! command failed ({returncode}): {printable}\n{stderr}")
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def snapshot_tables(binary: str, store: str) -> dict[str, int]:
    """Return per-table row counts for ``store`` — the baseline and the check."""
    out = run([binary, "snapshot", "--store", store]).stdout
    return {m["table"]: int(m["rows"]) for m in SNAPSHOT_ROW_RE.finditer(out)}


def snapshot_schema_version(binary: str, store: str) -> int | None:
    """Return the on-disk format version ``store`` is stamped at."""
    out = run([binary, "snapshot", "--store", store]).stdout
    match = SNAPSHOT_SCHEMA_RE.search(out)
    return int(match.group(1)) if match else None


def graph_ids_from_cluster_config(cluster_yaml: Path) -> list[str]:
    """Return the graph ids the live cluster declares.

    Read from the mounted cluster.yaml rather than re-derived from
    ``managed_repos``, for the reason the runbook gives: the ``code-<repo>``
    ids are *derived* by ``code_graph_id()``, and rebuilding that list by hand
    is how a repo gets dropped from the migration and left behind at the old
    root.

    Parsed with a small indentation-aware scan rather than PyYAML. The server
    image carries ``python3-yaml`` today, but this is the one input whose
    mis-parse silently shortens the migration, and the ``graphs:`` block is a
    flat map of ``<id>:`` keys — cheap to read exactly, and worth not resting
    on an image detail. A top-level key ends the block, which is what keeps the
    ``policies:`` entries below it from being read as graphs.
    """
    ids: list[str] = []
    in_graphs = False
    for raw in cluster_yaml.read_text().splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if not line[:1].isspace():
            in_graphs = line.startswith("graphs:")
            continue
        if in_graphs and re.fullmatch(r"  [A-Za-z0-9][A-Za-z0-9._-]*:", line):
            ids.append(line.strip().rstrip(":"))
    return ids


def build_rebuild_config(
    source_cluster_yaml: Path, rebuild_dir: Path, schema_dir: Path, new_root: str
) -> Path:
    """Stage a cluster config declaring the same graphs at ``new_root``.

    The schemas come from the image (baked in beside the server) and the graph
    declarations from the live ConfigMap, so the rebuilt cluster declares
    exactly what the running one does — same ids, same schemas, new root.

    ``new_root`` is re-validated here rather than trusted from the caller. The
    check below compares the written line against ``f"storage: {new_root}"``,
    which for an empty root is ``"storage: "`` — the very line the check exists
    to reject, so a blank value would pass its own guard. That is the runbook's
    documented worst case (``cluster validate`` also accepts a blank
    ``storage:``), and a guard that is self-consistently wrong for its one
    target input is more dangerous than no guard, because it reads as
    protection.
    """
    if not NEW_ROOT_RE.match(new_root):
        sys.exit(
            f"!!! refusing to stage a rebuild config for root {new_root!r} — "
            "expected s3://<bucket>/fmt<N>"
        )
    rebuild_dir.mkdir(parents=True, exist_ok=True)
    for schema in schema_dir.glob("*.pg"):
        shutil.copy2(schema, rebuild_dir / schema.name)

    lines = source_cluster_yaml.read_text().splitlines()
    repointed = [
        f"storage: {new_root}" if line.startswith("storage:") else line
        for line in lines
    ]
    target = rebuild_dir / "cluster.yaml"
    target.write_text("\n".join(repointed) + "\n")

    if f"storage: {new_root}" not in target.read_text().splitlines():
        sys.exit(f"!!! storage: was not repointed to {new_root} — refusing to continue")
    declared = sum(1 for line in lines if line.strip().startswith("schema:"))
    rebuilt = sum(
        1
        for line in target.read_text().splitlines()
        if line.strip().startswith("schema:")
    )
    if declared != rebuilt:
        sys.exit(
            f"!!! rebuilt config declares {rebuilt} schemas, source declared "
            f"{declared} — the repoint changed more than the storage line"
        )
    return target


def baseline_and_export(
    old_binary: str, old_root: str, graphs: list[str], export_dir: Path
) -> dict[str, dict[str, int]]:
    """Record per-table row counts and export every graph, with the OLD binary.

    Every read is against the old root, which nothing writes to for the whole
    procedure. A failure here is a clean stop: the new root does not exist yet
    and the live cluster is untouched.
    """
    LOG.info("=== 1/3 baseline + export (old binary)")
    export_dir.mkdir(parents=True, exist_ok=True)
    baseline: dict[str, dict[str, int]] = {}
    for graph in graphs:
        store = f"{old_root}/graphs/{graph}.omni"
        LOG.info("  -- %s", graph)
        baseline[graph] = snapshot_tables(old_binary, store)
        target = export_dir / f"{graph}.jsonl"
        run([old_binary, "export", "--store", store], stdout_path=target)
        with target.open() as fh:
            lines = sum(1 for _ in fh)
        LOG.info(
            "     %d rows across %d tables -> %d JSONL lines",
            sum(baseline[graph].values()),
            len(baseline[graph]),
            lines,
        )
    return baseline


def rebuild(  # noqa: PLR0913
    new_binary: str,
    new_root: str,
    graphs: list[str],
    export_dir: Path,
    rebuild_dir: Path,
    cluster_yaml: Path,
    schema_dir: Path,
    actor: str,
) -> None:
    """Create the graphs at ``new_root`` and load every export into them."""
    LOG.info("=== 2/3 rebuild (new binary)")
    config = build_rebuild_config(cluster_yaml, rebuild_dir, schema_dir, new_root)
    LOG.info("  staged %s -> storage: %s", config, new_root)
    run([new_binary, "cluster", "validate", "--config", str(rebuild_dir)])

    # `import` BEFORE `apply`, and both are required. A fresh root has no
    # `__cluster/state.json`, and `apply` refuses to create one
    # (`state_missing ... run cluster import to bootstrap state`). This bites
    # only on the first apply against a new root — which is exactly what this
    # is, at the point of no return.
    run([new_binary, "cluster", "import", "--config", str(rebuild_dir), "--as", actor])
    run([new_binary, "cluster", "apply", "--config", str(rebuild_dir), "--as", actor])

    for graph in graphs:
        LOG.info("  -- %s", graph)
        # `merge` into a freshly-created empty graph is a full load and is the
        # safe choice: `overwrite` is destructive and buys nothing against an
        # empty table. `--yes` because a non-local destructive write refuses
        # without a TTY, and there is none here.
        run(
            [
                new_binary,
                "load",
                "--store",
                f"{new_root}/graphs/{graph}.omni",
                "--data",
                str(export_dir / f"{graph}.jsonl"),
                "--mode",
                "merge",
                "--yes",
            ]
        )


def verify(
    new_binary: str,
    new_root: str,
    graphs: list[str],
    baseline: dict[str, dict[str, int]],
) -> tuple[dict[str, GraphReport], list[str]]:
    """Compare per-table row counts at the new root against the baseline."""
    LOG.info("=== 3/3 verify (per-table row counts)")
    report: dict[str, GraphReport] = {}
    mismatched: list[str] = []
    for graph in graphs:
        after = snapshot_tables(new_binary, f"{new_root}/graphs/{graph}.omni")
        before = baseline[graph]
        ok = after == before
        report[graph] = {
            "ok": ok,
            "before": before,
            "after": after,
            "missing_tables": sorted(set(before) - set(after)),
            "changed_tables": sorted(
                t for t in set(before) & set(after) if before[t] != after[t]
            ),
        }
        if not ok:
            mismatched.append(graph)
        LOG.info(
            "  %s %s: %d rows", "OK  " if ok else "FAIL", graph, sum(after.values())
        )
    return report, mismatched


def main() -> int:
    """Run one full rebuild-and-verify pass, returning a process exit status."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    old_binary = env("OMNIGRAPH_OLD_BINARY")
    new_binary = env("OMNIGRAPH_NEW_BINARY", "omnigraph")
    old_root = env("OMNIGRAPH_OLD_ROOT").rstrip("/")
    new_root = env("OMNIGRAPH_NEW_ROOT").rstrip("/")
    actor = env("OMNIGRAPH_MIGRATION_ACTOR")
    cluster_yaml = Path(env("OMNIGRAPH_CLUSTER_CONFIG"))
    schema_dir = Path(env("OMNIGRAPH_SCHEMA_DIR"))
    export_dir = Path(env("OMNIGRAPH_EXPORT_DIR", "/tmp/export"))  # noqa: S108
    rebuild_dir = Path(env("OMNIGRAPH_REBUILD_DIR", "/tmp/rebuild"))  # noqa: S108

    if not NEW_ROOT_RE.match(new_root):
        sys.exit(
            f"!!! OMNIGRAPH_NEW_ROOT {new_root!r} is malformed — expected "
            "s3://<bucket>/fmt<N> with real digits"
        )
    if not new_root.startswith(f"{old_root}/"):
        sys.exit(
            f"!!! OMNIGRAPH_NEW_ROOT {new_root!r} must be a prefix inside "
            f"{old_root!r}. Rebuilding outside the managed bucket loses the "
            "IRSA grant and the backups."
        )

    for label, binary in (("old", old_binary), ("new", new_binary)):
        reported = run([binary, "version"]).stdout.strip().replace("\n", " | ")
        LOG.info("%s: %s", label, reported)

    graphs = graph_ids_from_cluster_config(cluster_yaml)
    if not graphs:
        sys.exit(f"!!! no graphs declared in {cluster_yaml} — nothing to migrate")
    LOG.info("%d graph(s) to rebuild: %s", len(graphs), ", ".join(graphs))

    baseline = baseline_and_export(old_binary, old_root, graphs, export_dir)
    rebuild(
        new_binary,
        new_root,
        graphs,
        export_dir,
        rebuild_dir,
        cluster_yaml,
        schema_dir,
        actor,
    )
    report, mismatched = verify(new_binary, new_root, graphs, baseline)

    VERDICT_PATH.write_text(
        json.dumps(
            {
                "ok": not mismatched,
                "old_root": old_root,
                "new_root": new_root,
                "graphs": report,
                "new_internal_schema": snapshot_schema_version(
                    new_binary, f"{new_root}/graphs/{graphs[0]}.omni"
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )

    if mismatched:
        LOG.error(
            "VERIFICATION FAILED for: %s. The old root is untouched and the "
            "cluster still points at it — this is a clean stop. Do NOT "
            "repoint. Exports are on this pod at %s; inspect before deleting "
            "the Job.",
            ", ".join(mismatched),
            export_dir,
        )
        return 1

    LOG.info(
        "All %d graph(s) rebuilt and verified at %s. NOT DONE YET — the "
        "cluster still serves the OLD root. To cut over: pulumi config set "
        "omnigraph:storage_prefix %s --stack <CI|QA|Production>, then deploy "
        "the new image, verify, and soak before retiring the old root.",
        len(graphs),
        new_root,
        new_root.rsplit("/", 1)[-1],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
