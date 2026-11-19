"""Tests for the witan memory-reach dashboard.

A Loki panel with the wrong datasource or selector renders "No data" with no
error, which on this dashboard reads the same as memories going unread.
"""

from typing import Any

from ol_infrastructure.infrastructure.grafana_alerting.dashboards import (
    base,
    witan_memory,
)
from ol_infrastructure.infrastructure.grafana_alerting.dashboards.datasources import (
    LOKI_DATASOURCE_REF,
)


def _panels() -> list[dict[str, Any]]:
    return witan_memory._dashboard_json(
        base._timeseries_panel,
        base._stat_panel,
        base._logs_panel,
        base._row_panel,
    )["panels"]


def test_every_query_reads_the_witan_container_in_loki() -> None:
    """The container is `mcp`; `witan-server` is the app label, which reads zero."""
    queried = [panel for panel in _panels() if panel["type"] != "row"]

    assert queried
    for panel in queried:
        assert panel["datasource"] == LOKI_DATASOURCE_REF
        for target in panel["targets"]:
            assert target["datasource"] == LOKI_DATASOURCE_REF
            assert '{namespace="witan", container="mcp"}' in target["expr"]


def test_the_claim_share_counts_only_first_claims_on_both_sides() -> None:
    """The denominator keeps the claims that surfaced nothing."""
    share = next(
        panel
        for panel in _panels()
        if panel["title"] == "First claims that carried a memory"
    )

    numerator, denominator = share["targets"][0]["expr"].split(" / ")

    assert 'count != "0"' in numerator
    assert 'count != "0"' not in denominator
    assert "witan.memory.surfaced" in denominator


def test_the_claim_share_reads_zero_rather_than_no_data() -> None:
    """An empty numerator would blank the panel, and No data here means the
    event is not deployed yet.
    """
    share = next(
        panel
        for panel in _panels()
        if panel["title"] == "First claims that carried a memory"
    )

    assert "or vector(0)" in share["targets"][0]["expr"].split(" / ")[0]


def test_trailing_windows_do_not_sum_their_legend() -> None:
    """A `[1d]` window overlaps itself at Grafana's step, so a `sum` legend adds
    each read many times: about 5,000 for a 70-read week.
    """
    trailing = [
        panel
        for panel in _panels()
        if panel["type"] == "timeseries"
        and any("[1d]" in target["expr"] for target in panel["targets"])
    ]

    assert trailing
    for panel in trailing:
        assert panel["options"]["legend"]["calcs"] == ["lastNotNull"]


def test_panels_do_not_overlap() -> None:
    """Grafana repacks overlapping panels on load rather than rejecting them."""
    cells: set[tuple[int, int]] = set()
    for panel in _panels():
        grid = panel["gridPos"]
        for x in range(grid["x"], grid["x"] + grid["w"]):
            for y in range(grid["y"], grid["y"] + grid["h"]):
                assert (x, y) not in cells, panel["title"]
                cells.add((x, y))
