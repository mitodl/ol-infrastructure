"""Tests for the witan tool-call error-rate expression.

A ratio over a handful of calls pages on arithmetic: four rejected calls in a
quiet hour are a 100% failure rate.
"""

from ol_infrastructure.infrastructure.grafana_alerting.metric_rules import witan


def test_the_ratio_is_gated_on_a_minimum_call_count() -> None:
    """The gate counts every outcome; the ratio stays the left operand."""
    ratio, gate = witan._error_ratio_expr(witan._PROD_CLUSTERS).split(
        "and on (cluster)"
    )

    assert f"> {witan._ERROR_RATIO_THRESHOLD}" in ratio
    assert "increase(witan_tool_calls_total" in gate
    assert 'outcome="error"' not in gate
    assert gate.rstrip().endswith(f">= {witan._MIN_CALLS}")


def test_the_gate_reads_the_same_clusters_and_window_as_the_ratio() -> None:
    """A gate over another window or cluster set would not bound the ratio."""
    expr = witan._error_ratio_expr(witan._NON_PROD_CLUSTERS)

    assert expr.count(f'cluster=~"{witan._NON_PROD_CLUSTERS}"') == 3
    assert expr.count("[10m]") == 3
