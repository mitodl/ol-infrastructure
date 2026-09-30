"""Tests for the user-journey dashboard renderer.

The escaping and the grid cursor are both things that fail *silently* in
Grafana -- a mis-escaped filter renders "No data" with no error, and overlapping
panels get repacked on load rather than rejected -- so they are tested here
rather than left to be noticed on a dashboard nobody is watching closely.

`test_escaped_alternation_matches_only_the_literals` is the important one: it
reproduces, in Python's `re`, the two unescaping steps a query goes through
between this module and RE2 (PromQL's Go string literal, then the regex
engine), and asserts the result matches the endpoint patterns and nothing else.
"""

import re
from typing import Any

import pytest

from ol_infrastructure.infrastructure.grafana_alerting.dashboards import base
from ol_infrastructure.infrastructure.grafana_alerting.dashboards import (
    user_journey as uj,
)
from ol_infrastructure.infrastructure.grafana_alerting.dashboards.journeys import (
    JOURNEYS,
    MIT_LEARN_ORGANIZATION_DASHBOARD,
)
from ol_infrastructure.infrastructure.grafana_alerting.dashboards.user_journey import (
    _DEFAULT_RANGE_HOURS,
    _TEMPO_METRICS_MAX_HOURS,
    Journey,
    JourneyStep,
    _Layout,
    _regex_escape,
)

# Real `http_target` values read off the production stack, chosen because each
# one carries a different awkward character: regex anchors, a capture group, a
# backslash escape, a character class, and a plain literal.
REAL_TARGETS = [
    "api/v2/courses/$",
    "^api/v0/users/me/$",
    "api/v0/users/me",
    "api/v3/courses/variant_runs/",
    r"^logout\/?$",
    r"documents/(\d+)/(.*)$",
    r"^((?:[\w\-]+/)*)$",
    r"health/readiness\/?",
    "api/v0/b2b/manager/organizations/(?P<parent_lookup_organization>[^/.]+)/",
]


# Grafana accepts m/h/d/w suffixes on a relative range, so the default-range
# assertion converts rather than assuming hours: a default of "now-2d" is the
# bug that test exists to catch, and slicing an "h" off it would raise
# ValueError instead of failing with a readable message.
_RANGE_UNIT_HOURS = {"m": 1 / 60, "h": 1, "d": 24, "w": 24 * 7}


def _relative_range_hours(raw: str) -> float:
    """Convert a Grafana relative range like "now-24h" to hours."""
    match = re.fullmatch(r"now-(\d+)([mhdw])", raw)
    assert match, f"unrecognised relative range {raw!r}"
    return int(match.group(1)) * _RANGE_UNIT_HOURS[match.group(2)]


def unescape_promql_string(literal: str) -> str:
    r"""Apply the unescaping PromQL performs on a double-quoted string.

    PromQL string literals follow Go's rules, so `\\` collapses to `\` and a
    backslash before anything that is not a recognised escape is a parse error.
    Modelling that here is what lets the test below prove the *query text* is
    valid, not merely that the Python string looks plausible.
    """
    out = []
    index = 0
    while index < len(literal):
        character = literal[index]
        if character != "\\":
            out.append(character)
            index += 1
            continue
        if index + 1 >= len(literal):
            msg = "string ends in a lone backslash"
            raise ValueError(msg)
        following = literal[index + 1]
        if following == "\\":
            out.append("\\")
            index += 2
            continue
        msg = f"unknown escape sequence in PromQL string literal: \\{following}"
        raise ValueError(msg)
    return "".join(out)


@pytest.mark.parametrize("target", REAL_TARGETS)
def test_escaped_literal_survives_both_layers(target: str) -> None:
    """An escaped literal unescapes to a regex matching exactly itself."""
    regex = unescape_promql_string(_regex_escape(target))
    assert re.fullmatch(regex, target), (
        f"{target!r} escaped to {regex!r}, which does not match it"
    )


def test_escaped_alternation_matches_only_the_literals() -> None:
    """The journey's alternation matches its endpoints and nothing adjacent."""
    journey = Journey(
        uid="test",
        title="test",
        slug="test",
        description="",
        primary_service="svc",
        focus_step=REAL_TARGETS[0],
        steps=[JourneyStep(label=t, service="svc", target=t) for t in REAL_TARGETS],
    )
    # Prometheus anchors `=~` as ^(?:...)$, so model that too.
    regex = re.compile(f"^(?:{unescape_promql_string(journey.target_regex)})$")
    for target in REAL_TARGETS:
        assert regex.match(target), f"alternation failed to match {target!r}"

    # The near-misses that an unescaped pattern would wrongly match: dropping a
    # trailing `$` anchor, or letting `.`/`\d` behave as metacharacters.
    for near_miss in [
        "api/v2/courses/",
        "^api/v0/users/me/",
        "api/v0/usersXme",
        "documents/(X+)/(.*)$",
        "health/readinessX",
    ]:
        assert not regex.match(near_miss), f"alternation wrongly matched {near_miss!r}"


def test_backslash_escapes_to_four_not_three() -> None:
    """A literal backslash needs four in the query text, not the obvious three.

    Three produces `\\` followed by a stray escape, which Mimir rejects with
    `unknown escape sequence` and which takes out every panel sharing the
    filter, not just the offending step.
    """
    assert _regex_escape("\\") == "\\\\\\\\"
    unescape_promql_string(_regex_escape(r"^logout\/?$"))  # must not raise


def test_journey_rejects_duplicate_targets() -> None:
    """Two steps on one target would silently merge into one table row."""
    with pytest.raises(ValueError, match="distinct http_target"):
        Journey(
            uid="test",
            title="test",
            slug="test",
            description="",
            primary_service="svc",
            focus_step="api/v2/courses/$",
            steps=[
                JourneyStep(label="a", service="one", target="api/v2/courses/$"),
                JourneyStep(label="b", service="two", target="api/v2/courses/$"),
            ],
        )


def test_journey_rejects_focus_step_that_is_not_a_step() -> None:
    """A typo'd focus_step would open the dashboard on an empty endpoint."""
    with pytest.raises(ValueError, match="not one of"):
        Journey(
            uid="test",
            title="test",
            slug="test",
            description="",
            primary_service="svc",
            focus_step="api/v2/nope/$",
            steps=[JourneyStep(label="a", service="one", target="api/v2/courses/$")],
        )


def test_journey_rejects_a_backtick_in_a_target() -> None:
    """A backtick is the one character the trace panels' raw string cannot hold."""
    with pytest.raises(ValueError, match="backtick"):
        Journey(
            uid="test",
            title="test",
            slug="test",
            description="",
            primary_service="svc",
            focus_step="api/v2/`courses`/$",
            steps=[JourneyStep(label="a", service="one", target="api/v2/`courses`/$")],
        )


def test_trace_panels_use_a_raw_string_for_the_span_name() -> None:
    """Double-quoted TraceQL interprets escapes and breaks on a backslash.

    `^logout\\/?$` is a real http_target on mitxonline-webapp; querying it with
    a double-quoted span name fails with `parse error: invalid char escape`.
    """
    dashboard = render(MIT_LEARN_ORGANIZATION_DASHBOARD)
    trace_queries = [
        target["query"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if target.get("queryType") == "traceql"
    ]
    assert trace_queries, "no TraceQL panels rendered"
    for query in trace_queries:
        assert "name=`GET $endpoint`" in query
        assert 'name="GET' not in query


def test_default_range_lets_the_trace_row_render() -> None:
    """Tempo rejects a metrics query past 25h, so the default must sit under it.

    This is the bug the dashboard shipped with: the default was 48h, both
    TraceQL panels exceeded the cap, and they rendered blank with the error
    reachable only by inspecting the panel.
    """
    assert _DEFAULT_RANGE_HOURS < _TEMPO_METRICS_MAX_HOURS
    dashboard = render(MIT_LEARN_ORGANIZATION_DASHBOARD)
    time_range = dashboard["time"]
    assert time_range["to"] == "now"
    hours = _relative_range_hours(time_range["from"])
    assert hours < _TEMPO_METRICS_MAX_HOURS, (
        f"default range of {time_range['from']} exceeds Tempo's metrics cap"
    )


def test_traceql_panels_follow_the_dashboard_range() -> None:
    """No panel-level time override on the trace panels, deliberately.

    A `timeFrom` pin resolves to now-24h..now wherever the dashboard's window
    sits, so on a past range it renders a populated panel answering a different
    question. It is also inert when the dashboard range is absolute, which is
    the case it would have been added to rescue. Following the dashboard means
    these panels either show the selected range or show Tempo's error, and
    never wrong data. See _traceql_timeseries_panel in base.py.
    """
    dashboard = render(MIT_LEARN_ORGANIZATION_DASHBOARD)

    def walk(panels: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # A collapsed row nests its children under row["panels"], so a flat
        # scan would stop seeing the trace row the moment it was collapsed.
        found = []
        for panel in panels:
            found.append(panel)
            found.extend(walk(panel.get("panels", [])))
        return found

    traceql_panels = [
        panel
        for panel in walk(dashboard["panels"])
        if any(t.get("queryType") == "traceql" for t in panel.get("targets", []))
    ]
    # Without this the test passes vacuously the moment the trace panels move
    # or stop tagging queryType, which is exactly when it needs to fail.
    assert traceql_panels, "found no TraceQL panels to check"
    for panel in traceql_panels:
        assert "timeFrom" not in panel, (
            f"{panel['title']!r} pins its own window; it would show the "
            "last 24h while the rest of the dashboard shows another range"
        )
        assert "timeShift" not in panel


def test_layout_advances_by_the_tallest_panel_in_a_row() -> None:
    """Advancing by the last panel placed would overlap a taller neighbour."""
    layout = _Layout()
    tall = layout.place(width=12, height=10, x=0)
    short = layout.place(width=12, height=4, x=12)
    following = layout.place(width=24, height=3, x=0)
    assert tall["y"] == short["y"] == 0
    assert following["y"] == 10, "next row landed on top of the taller panel"


def test_layout_closes_an_unfinished_row_before_a_divider() -> None:
    """A half-width panel with no partner must not be drawn over."""
    layout = _Layout()
    orphan = layout.place(width=12, height=6, x=0)
    divider_y = layout.row()
    assert divider_y >= orphan["y"] + orphan["h"]


def test_layout_rejects_a_panel_that_overflows_the_grid() -> None:
    with pytest.raises(ValueError, match="overflows"):
        _Layout().place(width=12, height=4, x=18)


@pytest.mark.parametrize("journey", JOURNEYS, ids=lambda j: j.slug)
def test_configured_journeys_have_no_overlapping_panels(journey: Journey) -> None:
    """Every shipped dashboard lays out cleanly on Grafana's 24-column grid."""
    dashboard = render(journey)
    occupied: dict[tuple[int, int], str] = {}
    for panel in dashboard["panels"]:
        position = panel["gridPos"]
        assert position["x"] + position["w"] <= 24
        for x in range(position["x"], position["x"] + position["w"]):
            for y in range(position["y"], position["y"] + position["h"]):
                assert (x, y) not in occupied, (
                    f"{panel['title']!r} overlaps {occupied[(x, y)]!r} at {(x, y)}"
                )
                occupied[(x, y)] = panel["title"]


def render(journey: Journey) -> dict[str, Any]:
    """Render a journey through the real panel helpers."""
    return uj._dashboard_json(
        journey,
        base._timeseries_panel,
        base._stat_panel,
        base._table_panel,
        base._traceql_timeseries_panel,
        base._row_panel,
    )


def test_journey_filter_is_inlined_not_a_dashboard_variable() -> None:
    """The endpoint regex must not travel through Grafana interpolation.

    Grafana's Prometheus datasource escapes interpolated variable values on the
    way out, which would double the backslashes a second time and leave every
    filtered panel silently empty. Keeping the regex out of `templating` is
    what makes the query text testable against the datasource directly.
    """
    dashboard = render(MIT_LEARN_ORGANIZATION_DASHBOARD)
    variable_names = {v["name"] for v in dashboard["templating"]["list"]}
    assert "steps" not in variable_names
    assert "services" not in variable_names

    regex = MIT_LEARN_ORGANIZATION_DASHBOARD.target_regex
    inlined = [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if "expr" in target and regex in target["expr"]
    ]
    assert inlined, "no panel carries the inlined journey regex"
