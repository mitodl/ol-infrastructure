"""Render one dashboard per user journey, built for release comparison.

A per-service RED dashboard answers "is mitxonline-webapp healthy". It cannot
answer "did the organization dashboard get faster", because that capability's
cost is spread across nine endpoints on two services and the one that matters
is buried among ~120 others. These dashboards invert that: the journey is the
subject, and every panel is already filtered to it.

HOW BEFORE/AFTER WORKS: the panels split on `service_version`, the OTel
resource attribute carrying the app's release. Deploys are clean cutovers --
over the 7 days to 2026-09-17 production moved mitxonline-webapp 1.165.4 →
1.166.0 → .1 → .2 → .3 → .4 → .5 → .7 with only a single scrape interval of
overlap at each step -- so selecting the release before and the release after
puts two non-overlapping populations on one graph. That is stronger than
eyeballing a time range around a deploy: it cannot be thrown off by not knowing
exactly when the rollout finished, and a version that was rolled back shows up
as its own series rather than silently contaminating the "after" window.

WHY A TRACE ROW: latency says whether a fix worked, not why. For a change whose
mechanism is "stop issuing one query per row", the mechanism is visible as the
count of Postgres child spans under the request span, which is a structural
TraceQL query. Read the caveat on that row's panels -- traces are tail-sampled
and the absolute number is biased, the change between releases is not.

WHY THE JOURNEY FILTER IS INLINED RATHER THAN A DASHBOARD VARIABLE: the set of
endpoints is fixed at deploy time, so it is baked into each expression instead
of held in a `constant` variable. That is not a style choice. The endpoint
patterns contain backslashes, and Grafana's Prometheus datasource runs
interpolated variable values through its own escaping on the way out, which
would double those backslashes again and turn `\\^api/...` into an unmatchable
pattern -- silently, with every panel reading "No data" and no error anywhere.
Inlining removes that layer, so the query text that ships is the query text that
can be tested directly against the datasource.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pulumi import Input, ResourceOptions

from ol_infrastructure.infrastructure.grafana_alerting.dashboards.datasources import (
    MIMIR_DATASOURCE_REF,
)
from ol_infrastructure.infrastructure.grafana_alerting.dashboards.promql import (
    DURATION_METRIC,
    ZERO,
    quantile,
    rate,
)

_GRID_WIDTH = 24

# Tempo rejects a TraceQL metrics query spanning more than 25 hours:
#   metrics query time range exceeds the maximum allowed duration of 25h0m0s
# The trace row follows the dashboard's range (see _traceql_timeseries_panel in
# base.py for why it is not pinned), so this default has to sit under the cap or
# that row ships blank -- which is exactly how it shipped in #5920.
_TEMPO_METRICS_MAX_HOURS = 25
_DEFAULT_RANGE_HOURS = 24
_DEFAULT_RANGE = f"now-{_DEFAULT_RANGE_HOURS}h"

# RE2's metacharacters. The endpoint patterns these dashboards filter on are
# themselves Django URL regexes (`api/v2/courses/$`, `^api/v0/users/me/$`), so
# dropping them into a PromQL `=~` alternation unescaped silently matches the
# wrong thing rather than erroring: `api/v2/courses/$` read as a regex is an
# anchored match for `api/v2/courses/`, which is not a label value that exists.
_RE2_METACHARACTERS = r"\.+*?()|[]{}^$"


def _regex_escape(literal: str) -> str:
    r"""Escape a literal so it matches only itself in a PromQL `=~` matcher.

    Two layers of escaping stack here, and getting only the inner one right
    fails loudly while getting neither right fails silently.

    The inner layer is RE2, which needs `^` written as `\^`. The outer layer is
    PromQL's string literal, which follows Go's rules: `\^` inside double quotes
    is not a recognised escape sequence, and Mimir rejects the whole query with
    `parse error: unknown escape sequence U+005E '^'`. Every backslash the regex
    needs therefore has to be written twice in the query text.

    So an ordinary metacharacter `c` is emitted as `\\c` (RE2 sees `\c`), and a
    literal backslash is emitted as `\\\\` (RE2 sees `\\`, which matches one
    backslash). Emitting three backslashes for the backslash case -- the obvious
    off-by-one -- produces `\\` followed by a stray `\d`, which is again not a
    valid Go escape and takes down every panel sharing the filter.

    None of this journey's nine endpoints contain a backslash, but plenty of
    real `http_target` values do (`^logout\/?$`, `documents/(\d+)/(.*)$`), so
    the next journey appended to `journeys.py` is where it would have bitten.

    The doubled form is verified against the datasource in
    tests/ol_infrastructure/infrastructure/grafana_alerting/test_user_journey.py
    and, on 2026-09-17, against the production stack via /api/ds/query: the
    single-backslash form returns HTTP 400 for every query using it, the doubled
    form returns all nine of this journey's endpoints.
    """
    escaped = []
    for character in literal:
        if character == "\\":
            escaped.append("\\\\\\\\")
        elif character in _RE2_METACHARACTERS:
            escaped.append(f"\\\\{character}")
        else:
            escaped.append(character)
    return "".join(escaped)


@dataclass(frozen=True)
class JourneyStep:
    """One HTTP endpoint the journey calls.

    :param label: Human name for the step, used in panel descriptions.
    :param service: `service_name` resource attribute of the app serving it.
    :param target: The `http_target` label value, i.e. the Django URL pattern.
    :param note: Why this call happens and anything that would otherwise make
        its panels read wrong (e.g. a lazily-fired call sitting near zero).
    """

    label: str
    service: str
    target: str
    note: str = ""


@dataclass(frozen=True)
class Journey:
    """A user-facing capability and the requests behind it.

    :param uid: Stable Grafana dashboard UID.
    :param title: Dashboard title.
    :param slug: Pulumi resource-name stem.
    :param description: One or two sentences shown on the dashboard itself.
    :param primary_service: Service the release-comparison row defaults to.
    :param focus_step: `target` of the step the single-endpoint panels open on.
    :param steps: The journey's endpoints, in the order the browser calls them.
    :param known_gaps: What this dashboard cannot see, stated on the dashboard
        rather than left for a reader to discover by misreading a panel.
    :raises ValueError: If two steps share a `target`, or `focus_step` is not
        one of the steps.
    """

    uid: str
    title: str
    slug: str
    description: str
    primary_service: str
    focus_step: str
    steps: list[JourneyStep]
    known_gaps: str = ""

    def __post_init__(self) -> None:
        """Enforce the invariants the panel aggregations depend on."""
        targets = [step.target for step in self.steps]
        duplicates = {target for target in targets if targets.count(target) > 1}
        if duplicates:
            # The overview table aggregates `by (http_target)` and joins its
            # columns on that one field, so two steps sharing a target would
            # silently sum two services' traffic into a single row rather than
            # failing. Rejecting it here is cheaper than defending every panel.
            msg = (
                f"{self.title}: steps must have distinct http_target values, "
                f"got duplicates {sorted(duplicates)}"
            )
            raise ValueError(msg)
        if self.focus_step not in targets:
            msg = (
                f"{self.title}: focus_step {self.focus_step!r} is not one of "
                f"the journey's steps"
            )
            raise ValueError(msg)
        backticked = [target for target in targets if "`" in target]
        if backticked:
            # The trace panels address spans as ``name=`GET <target>` `` so that
            # a backslash in the pattern survives; a backtick is the one
            # character that raw string cannot carry. No Django URL pattern has
            # one, so this is a guard against a typo rather than a real
            # restriction.
            msg = (
                f"{self.title}: http_target values cannot contain a backtick, "
                f"which would break the trace panels' TraceQL string: "
                f"{sorted(backticked)}"
            )
            raise ValueError(msg)

    @property
    def services(self) -> list[str]:
        """Distinct services the journey touches, in first-call order."""
        return list(dict.fromkeys(step.service for step in self.steps))

    @property
    def service_regex(self) -> str:
        """Alternation matching every service the journey touches."""
        return "|".join(_regex_escape(service) for service in self.services)

    @property
    def target_regex(self) -> str:
        """Alternation matching exactly this journey's endpoints."""
        return "|".join(_regex_escape(step.target) for step in self.steps)

    @property
    def step_table(self) -> str:
        """Markdown list of the steps, for the dashboard description."""
        lines = []
        for index, step in enumerate(self.steps, start=1):
            suffix = f" -- {step.note}" if step.note else ""
            lines.append(
                f"{index}. **{step.label}** (`{step.service}` `{step.target}`){suffix}"
            )
        return "\n".join(lines)


class _Layout:
    """Running y-cursor for Grafana's 24-column grid.

    Hand-written gridPos coordinates go stale the moment a panel is inserted
    above them, and Grafana repacks overlapping panels on load rather than
    erroring, so the symptom of a mistake is a scrambled dashboard rather than a
    failure anyone would notice in review.
    """

    def __init__(self) -> None:
        self.y = 0
        self._row_height = 0

    def _advance(self) -> None:
        self.y += self._row_height
        self._row_height = 0

    def row(self) -> int:
        """Reserve a full-width row divider (always 1 unit tall)."""
        if self._row_height:
            # A previous partial row never filled up; close it out rather than
            # drawing the divider on top of it.
            self._advance()
        y = self.y
        self.y += 1
        return y

    def place(self, *, width: int, height: int, x: int = 0) -> dict[str, int]:
        """Place a panel at the cursor; advance only when the row is full.

        Tracks the tallest panel in the current row rather than the last one
        placed: advancing by the last panel's height puts the next row on top of
        a taller neighbour that is still occupying those cells.
        """
        if x + width > _GRID_WIDTH:
            msg = (
                f"panel at x={x} width={width} overflows the {_GRID_WIDTH}-column grid"
            )
            raise ValueError(msg)
        position = {"h": height, "w": width, "x": x, "y": self.y}
        self._row_height = max(self._row_height, height)
        if x + width == _GRID_WIDTH:
            self._advance()
        return position


def _templating(journey: Journey) -> dict[str, Any]:
    """Build the dashboard variables.

    Only the things a reader legitimately changes are variables: which service,
    which two releases, which endpoint. The journey's own endpoint set is not a
    variable (see the module docstring on inlining).

    `endpoint` is queried rather than hardcoded so that it follows `service` and
    only ever offers endpoints that have data on the stack being viewed.
    """
    service_selector = f'service_name="$service", http_target=~"{journey.target_regex}"'
    release_query = (
        f'label_values({DURATION_METRIC}_count{{service_name="$service"}}, '
        "service_version)"
    )
    return {
        "list": [
            {
                "name": "service",
                "label": "Service",
                "type": "custom",
                "query": ",".join(journey.services),
                "current": {
                    "text": journey.primary_service,
                    "value": journey.primary_service,
                },
                "options": [
                    {
                        "text": service,
                        "value": service,
                        "selected": service == journey.primary_service,
                    }
                    for service in journey.services
                ],
            },
            {
                "name": "baseline",
                "label": "Release before",
                "type": "query",
                "datasource": MIMIR_DATASOURCE_REF,
                "query": release_query,
                # Natural-order descending, so 1.166.10 sorts above 1.166.9
                # rather than below it the way a plain string sort puts it.
                "sort": 8,
                "refresh": 2,
            },
            {
                "name": "candidate",
                "label": "Release after",
                "type": "query",
                "datasource": MIMIR_DATASOURCE_REF,
                "query": release_query,
                "sort": 8,
                "refresh": 2,
            },
            {
                "name": "endpoint",
                "label": "Endpoint",
                "type": "query",
                "datasource": MIMIR_DATASOURCE_REF,
                "query": f"label_values({DURATION_METRIC}_count{{{service_selector}}}, http_target)",
                "current": {
                    "text": journey.focus_step,
                    "value": journey.focus_step,
                },
                "refresh": 2,
            },
        ]
    }


def _comparison_panels(
    journey: Journey,
    layout: _Layout,
    timeseries_panel: Callable[..., dict[str, Any]],
    stat_panel: Callable[..., dict[str, Any]],
    table_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the before/after row for one release pair, by `service_version`."""
    endpoint = 'service_name="$service", http_target="$endpoint"'
    before = f'{endpoint}, service_version="$baseline"'
    after = f'{endpoint}, service_version="$candidate"'
    # $__range rather than $__rate_interval: each release only has samples
    # during the window it was live, so a rate over the dashboard's whole range
    # is the only window that covers both. It deflates the per-second rate for
    # a release that ended mid-range (the increase is spread over the full
    # window) but leaves the quantiles correct, because every histogram bucket
    # is deflated by the same factor.
    journey_scope = f'service_name="$service", http_target=~"{journey.target_regex}"'
    journey_before = f'{journey_scope}, service_version="$baseline"'
    journey_after = f'{journey_scope}, service_version="$candidate"'
    p95_before = quantile(0.95, journey_before, by="http_target", window="$__range")
    p95_after = quantile(0.95, journey_after, by="http_target", window="$__range")
    pick_two = (
        "Reads exactly zero when the two release pickers are on the same "
        "value, which is what they default to on first open. Set them to "
        "different releases before reading anything on this row."
    )

    return [
        row_panel(
            title=(
                "Release comparison - $service, $baseline vs $candidate "
                "(set these to two different releases)"
            ),
            y=layout.row(),
        ),
        stat_panel(
            title="p95 before - $endpoint",
            expr=quantile(0.95, before, window="$__range"),
            grid_pos=layout.place(width=6, height=4, x=0),
            unit="ms",
            decimals=0,
        ),
        stat_panel(
            title="p95 after - $endpoint",
            expr=quantile(0.95, after, window="$__range"),
            grid_pos=layout.place(width=6, height=4, x=6),
            unit="ms",
            decimals=0,
        ),
        stat_panel(
            title="Change in p95",
            expr=(
                f"(({quantile(0.95, after, window='$__range')}) - "
                f"({quantile(0.95, before, window='$__range')})) / "
                f"({quantile(0.95, before, window='$__range')})"
            ),
            grid_pos=layout.place(width=6, height=4, x=12),
            unit="percentunit",
            decimals=1,
        ),
        stat_panel(
            title="Requests compared",
            # `or vector(0)` on each term because these are label-less sums: a
            # release that never served this endpoint yields an empty vector,
            # and empty + anything is empty, so without it the panel reads "No
            # data" instead of the release that does have traffic.
            expr=(
                f"(sum(increase({DURATION_METRIC}_count{{{before}}}[$__range])) "
                f"{ZERO}) + "
                f"(sum(increase({DURATION_METRIC}_count{{{after}}}[$__range])) "
                f"{ZERO})"
            ),
            grid_pos=layout.place(width=6, height=4, x=18),
            unit="short",
            decimals=0,
        ),
        timeseries_panel(
            title="$endpoint latency by release",
            queries=[
                {
                    "expr": quantile(
                        quantile_value,
                        endpoint,
                        by="service_version",
                    ),
                    "legend_format": (
                        f"p{int(quantile_value * 100)} {{{{service_version}}}}"
                    ),
                }
                for quantile_value in (0.50, 0.95, 0.99)
            ],
            grid_pos=layout.place(width=12, height=8, x=0),
            unit="ms",
            legend_calc="max",
            description=(
                "Every release that served this endpoint in the window, not "
                "only the two selected above -- a regression introduced two "
                "releases before the one under test is visible here and "
                "invisible in the stats. Interpolated from the SDK's 15 "
                "default bucket boundaries, so a p99 inside the 2500-5000 ms "
                "bucket is precise to that bucket and no further."
            ),
        ),
        timeseries_panel(
            title="$endpoint request rate by release",
            expr=f"sum by (service_version) ({rate(endpoint)})",
            legend_format="{{service_version}}",
            grid_pos=layout.place(width=12, height=8, x=12),
            unit="reqps",
            legend_calc="mean",
            description=(
                "Read alongside the latency panel: a release that looks faster "
                "while also serving far less traffic has not necessarily been "
                "made faster."
            ),
        ),
        table_panel(
            title="Per-endpoint before/after on $service",
            join_field="http_target",
            sort_by="p95 after (ms)",
            columns=[
                {"expr": p95_before, "title": "p95 before (ms)", "unit": "ms"},
                {"expr": p95_after, "title": "p95 after (ms)", "unit": "ms"},
                {
                    "expr": f"({p95_after}) - ({p95_before})",
                    "title": "Change (ms)",
                    "unit": "ms",
                },
                {
                    "expr": f"(({p95_after}) - ({p95_before})) / ({p95_before})",
                    "title": "Change (%)",
                    "unit": "percentunit",
                },
                {
                    "expr": (
                        "sum by (http_target) "
                        f"(increase({DURATION_METRIC}_count{{{journey_after}}}"
                        "[$__range]))"
                    ),
                    "title": "Requests after",
                    "unit": "short",
                },
            ],
            grid_pos=layout.place(width=24, height=10, x=0),
            description=(
                "Whether the fix moved the endpoint it targeted, and whether "
                "it moved anything else. Covers this journey's steps on "
                "$service only, so switching the Service picker changes which "
                "steps appear. The change columns subtract two vectors matched "
                "on http_target, so an endpoint served by only one of the two "
                "releases is dropped from the row set rather than shown "
                "against a zero. Set the dashboard time range to span both "
                f"releases; each column aggregates over that whole range and "
                f"reads each release's own window out of it. {pick_two}"
            ),
        ),
    ]


def _trace_panels(
    layout: _Layout,
    traceql_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the database-work row, read from trace structure.

    These two panels are meant to be read as a pair and divided by eye: the
    left over the right is queries per request. Grafana will not do that
    division for you here. Its server-side expression engine can divide one
    query's series by another's, but the Tempo datasource returns an exemplar
    frame alongside every series frame and the expression engine rejects the
    mixed frame set (checked 2026-09-17 against the production stack via
    /api/ds/query: A and B each return 200 with three series and three
    `exemplar` frames, and `$A / $B` fails with `sse.dependencyError`;
    `exemplars: 0` on the target does not suppress them). Two honest panels
    beat one panel wired to an expression that silently yields nothing.
    """
    # The span name the wsgi instrumentation produces is the method plus the
    # same URL pattern that lands in `http_target`, so `$endpoint` addresses
    # spans and metric series alike. `>>` is the descendant operator, so this
    # counts the Postgres spans underneath *this endpoint's* request spans
    # rather than every Postgres span the service emits.
    # Backticks, not double quotes: TraceQL's double-quoted strings interpret
    # escape sequences the same way PromQL's do, so an endpoint pattern holding
    # a backslash (`^logout\/?$` is a real one on mitxonline-webapp) fails with
    # `parse error: invalid char escape`. A backtick string is raw. Checked
    # 2026-09-17 against the production stack: the double-quoted form errors on
    # that span name, the backtick form matches its 114 spans.
    #
    # This is strictly safer rather than provably airtight. The value arrives
    # through Grafana's variable interpolation, and whether the Tempo datasource
    # escapes it on the way out is frontend behaviour with no server-side probe
    # (the same layer that forced the journey regex to be inlined). Raw strings
    # cannot be worse: the double-quoted form breaks on a backslash regardless,
    # the raw form breaks only if Grafana escapes. `Journey` rejects a target
    # containing a backtick, which is the one character a raw string cannot
    # carry, and no URL pattern has one.
    request_spans = '{resource.service.name="$service" && name=`GET $endpoint`}'
    postgres_under_request = f'{request_spans} >> {{span.db.system="postgresql"}}'
    sampling_caveat = (
        "Traces are tail-sampled, so both panels describe the sampled "
        "fraction rather than all traffic, and the sampler favours slow "
        "requests, which are also the ones issuing the most queries. Read the "
        "ratio's change between releases rather than its absolute value, and "
        "only when latency is comparable across the pair: the sampler picks "
        "on latency, so a release that merely got faster shifts the sampled "
        "population toward cheaper requests and drops this ratio on its own, "
        "with no change in query count. That has already happened here once, "
        "across a Granian worker-config change. "
        f"Tempo rejects a metrics query spanning more than "
        f"{_TEMPO_METRICS_MAX_HOURS}h, so this row is blank whenever the "
        "dashboard's range is wider than that. It is blank rather than wrong. "
        "Keep the range under that cap to read it, which in practice means "
        "this row can only compare releases deployed within about a day of "
        "each other."
    )
    return [
        row_panel(title="Database work per request (traces)", y=layout.row()),
        traceql_panel(
            title="Postgres spans/sec under $endpoint, by release",
            queries=[
                f"{postgres_under_request} | rate() by (resource.service.version)"
            ],
            grid_pos=layout.place(width=12, height=8, x=0),
            unit="short",
            legend_calc="mean",
            description=(
                "The mechanism a latency panel cannot show. Divide this "
                "panel's mean by the mean on its right to get Postgres "
                "queries per request. A fix that makes query count flat in "
                "result-set size drops that ratio sharply while the panel on "
                "the right holds steady; that shape, not any particular "
                f"number, is what to look for. {sampling_caveat}"
            ),
        ),
        traceql_panel(
            title="Sampled requests/sec for $endpoint, by release",
            queries=[f"{request_spans} | rate() by (resource.service.version)"],
            grid_pos=layout.place(width=12, height=8, x=12),
            unit="reqps",
            legend_calc="mean",
            description=(
                "The denominator for the panel on the left, drawn from the "
                "same sampled traces so that the two divide cleanly. This is "
                "not the endpoint's real request rate -- that is on the "
                "release-comparison row above, from metrics, which bypass the "
                "sampler entirely."
            ),
        ),
    ]


def _overview_panels(
    journey: Journey,
    layout: _Layout,
    timeseries_panel: Callable[..., dict[str, Any]],
    table_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the whole-journey row, independent of any release pair."""
    scope = (
        f'service_name=~"{journey.service_regex}", '
        f'http_target=~"{journey.target_regex}"'
    )
    errors_5xx = f'{scope}, http_status_code=~"5.."'
    errors_4xx = f'{scope}, http_status_code=~"4.."'
    # $__range, not $__rate_interval, for the table: several journey steps are
    # low-traffic by design (variant runs fired 32 times in 7 days), and an
    # instant query over a ~1 minute window leaves those rows blank almost
    # always. The graphs below keep $__rate_interval because they plot shape
    # over time rather than one summary number.
    table_window = "$__range"
    return [
        row_panel(title="Journey overview - all steps, all releases", y=layout.row()),
        table_panel(
            title="Per-step RED",
            join_field="http_target",
            sort_by="p95 (ms)",
            columns=[
                {
                    "expr": (
                        f"sum by (http_target) ({rate(scope, window=table_window)})"
                    ),
                    "title": "Requests/sec",
                    "unit": "reqps",
                },
                {
                    "expr": quantile(
                        0.95, scope, by="http_target", window=table_window
                    ),
                    "title": "p95 (ms)",
                    "unit": "ms",
                },
                {
                    "expr": quantile(
                        0.99, scope, by="http_target", window=table_window
                    ),
                    "title": "p99 (ms)",
                    "unit": "ms",
                },
                {
                    # Deliberately no `or vector(0)`: vector(0) carries no
                    # labels, so it can never match a `by (http_target)`
                    # denominator. A step with no 5xx leaves the cell empty,
                    # which is honest, rather than joining onto every row.
                    "expr": (
                        "sum by (http_target) "
                        f"({rate(errors_5xx, window=table_window)}) / "
                        "sum by (http_target) "
                        f"({rate(scope, window=table_window)})"
                    ),
                    "title": "5xx ratio",
                    "unit": "percentunit",
                },
            ],
            grid_pos=layout.place(width=24, height=9, x=0),
            description=(
                "One row per journey step, aggregated over the dashboard's "
                "whole time range. These are whole-service figures for each "
                "endpoint, not only the requests this page made: nothing on "
                "the metric distinguishes a call from the organization "
                "dashboard from the same endpoint called elsewhere. For steps "
                "that only this journey calls the two are the same thing."
            ),
        ),
        timeseries_panel(
            title="p95 by step",
            expr=quantile(0.95, scope, by="service_name, http_target"),
            legend_format="{{service_name}} {{http_target}}",
            grid_pos=layout.place(width=12, height=8, x=0),
            unit="ms",
            legend_calc="max",
        ),
        timeseries_panel(
            title="Request rate by step",
            expr=f"sum by (service_name, http_target) ({rate(scope)})",
            legend_format="{{service_name}} {{http_target}}",
            grid_pos=layout.place(width=12, height=8, x=12),
            unit="reqps",
            legend_calc="mean",
            description=(
                "The steps should track each other, since the page calls them "
                "together. One step diverging means either a lazily-fired call "
                "(variant runs) or a call the page is now making more than "
                "once per load."
            ),
        ),
        timeseries_panel(
            title="Error rate by step",
            queries=[
                {
                    "expr": f"sum by (http_target) ({rate(errors_5xx)})",
                    "legend_format": "5xx {{http_target}}",
                },
                {
                    "expr": f"sum by (http_target) ({rate(errors_4xx)})",
                    "legend_format": "4xx {{http_target}}",
                },
            ],
            grid_pos=layout.place(width=24, height=8, x=0),
            unit="reqps",
            legend_calc="max",
            description=(
                "4xx is graphed next to 5xx because on these endpoints a large "
                "share of 4xx is 401/403 from unauthenticated polling, which "
                "is normal traffic. A 4xx step change is worth reading; a "
                "steady 4xx floor is not."
            ),
        ),
    ]


def _dashboard_json(
    journey: Journey,
    timeseries_panel: Callable[..., dict[str, Any]],
    stat_panel: Callable[..., dict[str, Any]],
    table_panel: Callable[..., dict[str, Any]],
    traceql_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    layout = _Layout()
    description = f"{journey.description}\n\n{journey.step_table}"
    if journey.known_gaps:
        description = f"{description}\n\nNot covered: {journey.known_gaps}"
    return {
        "uid": journey.uid,
        "title": journey.title,
        "description": description,
        "tags": ["user-journey", "opentelemetry", "release-comparison"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        # The widest default where every row works from one window (see
        # _TEMPO_METRICS_MAX_HOURS). Widening it is fine and sometimes
        # necessary, but costs two things worth knowing: the trace row goes
        # blank past 25h, and the release pickers are `label_values` variables
        # refreshed on time-range change, so they list only versions seen in
        # the selected window. To study a release older than the default,
        # narrow onto it rather than widening to reach back to it.
        "time": {"from": _DEFAULT_RANGE, "to": "now"},
        "refresh": "5m",
        "templating": _templating(journey),
        "panels": [
            *_comparison_panels(
                journey, layout, timeseries_panel, stat_panel, table_panel, row_panel
            ),
            *_trace_panels(layout, traceql_panel, row_panel),
            *_overview_panels(
                journey, layout, timeseries_panel, table_panel, row_panel
            ),
        ],
    }


def create(
    folder_uid: Input[str],
    journeys: list[Journey],
    timeseries_panel: Callable[..., dict[str, Any]],
    stat_panel: Callable[..., dict[str, Any]],
    table_panel: Callable[..., dict[str, Any]],
    traceql_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
    create_dashboard: Callable[
        [str, Input[str], dict[str, Any], ResourceOptions], None
    ],
    resource_opts: ResourceOptions,
) -> None:
    """Create one dashboard per journey."""
    for journey in journeys:
        create_dashboard(
            f"user-journey-{journey.slug}",
            folder_uid,
            _dashboard_json(
                journey,
                timeseries_panel,
                stat_panel,
                table_panel,
                traceql_panel,
                row_panel,
            ),
            resource_opts,
        )


__all__ = ["Journey", "JourneyStep", "create"]
