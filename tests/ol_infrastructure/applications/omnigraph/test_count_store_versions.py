"""Tests for the per-graph version census.

What the alert rules depend on is the split: a ``__manifest`` version on main
or on a branch tree is one cleanup can never remove, and a node or edge table
version is one it can. A key outside ``_versions/`` is neither.
"""

import subprocess

import pytest

from ol_infrastructure.applications.omnigraph.scripts import count_store_versions
from ol_infrastructure.applications.omnigraph.scripts.count_store_versions import (
    count_versions,
    main,
    render,
)

_COUNCIL = "2026-10-27 10:00:00 1 fmt14/graphs/council.omni"
_LISTING = [
    f"{_COUNCIL}/__manifest/_versions/1.manifest",
    f"{_COUNCIL}/__manifest/tree/act-1/_versions/2.manifest",
    f"{_COUNCIL}/nodes/ab12/_versions/3.manifest",
    f"{_COUNCIL}/edges/cd34/_versions/4.manifest",
    f"{_COUNCIL}/nodes/ab12/data/x.lance",
    f"{_COUNCIL}/__manifest/_transactions/1.txn",
    "2026-10-27 10:00:00 1 fmt14/graphs/code-bridge.omni/nodes/ef56/_versions/9.m",
    "",
]
_GRAPHS_URI = "s3://bucket/fmt14/graphs/"


def _fake_listing(stdout: str, returncode: int = 0):
    def run(args, **_kwargs):
        assert args[-1] == _GRAPHS_URI
        return subprocess.CompletedProcess(args, returncode, stdout=stdout)

    return run


def test_splits_manifest_and_table_versions_per_graph():
    """Main and branch-tree manifests are one kind, node and edge tables another."""
    counts = count_versions(_LISTING)

    assert counts["council"] == {"manifest": 2, "table": 2}
    assert counts["code-bridge"] == {"table": 1}


def test_renders_one_logfmt_line_per_graph_sorted():
    """Each line carries both kinds and their sum, which the rules unwrap."""
    assert render(count_versions(_LISTING)) == [
        "event=omnigraph_versions graph=code-bridge manifest_versions=0"
        " table_versions=1 versions=1",
        "event=omnigraph_versions graph=council manifest_versions=2"
        " table_versions=2 versions=4",
    ]


def test_main_prints_counts(monkeypatch, capsys):
    """The root's trailing slash does not change the listed prefix."""
    monkeypatch.setattr(
        count_store_versions.subprocess, "run", _fake_listing("\n".join(_LISTING))
    )

    assert main({"OMNIGRAPH_STORAGE_ROOT": "s3://bucket/fmt14/"}) == 0
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_main_fails_when_the_root_holds_no_graph(monkeypatch):
    """A wrong root would otherwise print nothing, which the rules read as OK.

    `aws s3 ls --recursive` on an empty prefix exits 1 with no output.
    """
    monkeypatch.setattr(
        count_store_versions.subprocess, "run", _fake_listing("", returncode=1)
    )

    assert main({"OMNIGRAPH_STORAGE_ROOT": "s3://bucket/fmt14"}) == 1


def test_main_fails_when_the_listing_fails(monkeypatch):
    """A failed listing fails the Job rather than printing a partial count."""

    monkeypatch.setattr(
        count_store_versions.subprocess, "run", _fake_listing("", returncode=253)
    )

    with pytest.raises(subprocess.CalledProcessError):
        main({"OMNIGRAPH_STORAGE_ROOT": "s3://bucket/fmt14"})
