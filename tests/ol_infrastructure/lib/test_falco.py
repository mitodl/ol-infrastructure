"""Run the falco VCL suites and guard how their main VCL is rendered."""

import re
from pathlib import Path

import pytest

from ol_infrastructure.lib.falco import (
    FALCO_RELEASES,
    REPO_ROOT,
    SUITES,
    VCL_TESTS_DIR,
    VclSnippet,
    VclSuite,
    ensure_falco,
    load_suite,
    render_main_vcl,
    run_suite,
)


@pytest.mark.parametrize("name", sorted(SUITES))
def test_suite_lints_and_passes(name: str):
    """Fails on any falco lint error or failing ``tests/vcl/<suite>`` test."""
    results = run_suite(load_suite(name))
    failures = [f"falco {r.command}:\n{r.output}" for r in results if not r.ok]
    assert not failures, "\n".join(failures)


def _renovate_falco_pattern() -> re.Pattern[str]:
    """Read the falco manager's matchString out of renovate.json5 as a Python regex."""
    line = next(
        line.strip()
        for line in REPO_ROOT.joinpath("renovate.json5").read_text().splitlines()
        if "github-release-attachments" in line
        and "matchString" not in line
        and line.strip().startswith("'")
    )
    pattern = line.rstrip(",").strip("'").replace("\\\\", "\\")
    return re.compile(pattern.replace("(?<", "(?P<"))


def test_renovate_manages_every_falco_platform():
    """Renovate only moves the entries its regex matches; a miss pins that platform."""
    matches = list(
        _renovate_falco_pattern().finditer(
            REPO_ROOT.joinpath("src/ol_infrastructure/lib/falco.py").read_text()
        )
    )
    assert [(m["currentValue"], m["currentDigest"]) for m in matches] == list(
        FALCO_RELEASES.values()
    )


def test_every_platform_pins_the_same_falco_release():
    assert len({version for version, _ in FALCO_RELEASES.values()}) == 1


def test_relative_falco_bin_is_resolved_before_the_cwd_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    binary = tmp_path.joinpath("tools", "falco")
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FALCO_BIN", "./tools/falco")
    assert ensure_falco() == binary.resolve()


def test_every_test_directory_has_a_suite():
    """A tests/vcl/<name>/ directory with no SUITES entry would never run."""
    directories = {path.name for path in VCL_TESTS_DIR.glob("*/")}
    assert directories <= set(SUITES)


def test_snippets_render_in_priority_order_within_their_subroutine():
    suite = VclSuite(
        name="ordering",
        snippets=[
            VclSnippet(name="late", type="recv", content="# late", priority=200),
            VclSnippet(name="default", type="recv", content="# default"),
            VclSnippet(name="early", type="recv", content="# early", priority=10),
            VclSnippet(name="fetch", type="fetch", content="# fetch"),
        ],
    )
    vcl = render_main_vcl(suite)
    recv = vcl[vcl.index("sub vcl_recv") : vcl.index("sub vcl_hash")]
    assert recv.index("# early") < recv.index("# default") < recv.index("# late")
    assert "# fetch" not in recv


def test_init_snippets_and_dictionaries_are_top_level():
    suite = VclSuite(
        name="tables",
        snippets=[VclSnippet(name="t", type="init", content="table t { }")],
        dictionaries={"redirects": {"/a": '301|keep|/c?d="e%20f"'}},
    )
    vcl = render_main_vcl(suite)
    assert vcl.index("table t { }") < vcl.index("sub vcl_recv")
    # A long string keeps quotes and %-escapes as written, like Fastly dictionaries
    assert 'table redirects {\n  "/a": {"301|keep|/c?d="e%20f""},\n}' in vcl


@pytest.mark.parametrize(
    ("items", "match"),
    [
        ({"k": 'a"}b'}, "long string"),
        ({"/a%20b": "v"}, "string literal"),
        ({'/a"b': "v"}, "string literal"),
    ],
)
def test_dictionary_entries_vcl_cannot_hold_are_rejected(
    items: dict[str, str], match: str
):
    suite = VclSuite(name="bad", snippets=[], dictionaries={"d": items})
    with pytest.raises(ValueError, match=match):
        render_main_vcl(suite)


def test_unknown_snippet_type_is_rejected():
    suite = VclSuite(
        name="bad",
        snippets=[VclSnippet(name="x", type="recieve", content="")],
    )
    with pytest.raises(ValueError, match="recieve"):
        render_main_vcl(suite)
