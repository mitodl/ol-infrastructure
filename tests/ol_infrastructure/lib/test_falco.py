"""Run the falco VCL suites and guard how their main VCL is rendered."""

import pytest

from ol_infrastructure.lib.falco import (
    SUITES,
    VCL_TESTS_DIR,
    VclSnippet,
    VclSuite,
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
        dictionaries={"redirects": {'/a"b': "301|keep|/c"}},
    )
    vcl = render_main_vcl(suite)
    assert vcl.index("table t { }") < vcl.index("sub vcl_recv")
    assert 'table redirects {\n  "/a\\"b": "301|keep|/c",\n}' in vcl


def test_unknown_snippet_type_is_rejected():
    suite = VclSuite(
        name="bad",
        snippets=[VclSnippet(name="x", type="recieve", content="")],
    )
    with pytest.raises(ValueError, match="recieve"):
        render_main_vcl(suite)
