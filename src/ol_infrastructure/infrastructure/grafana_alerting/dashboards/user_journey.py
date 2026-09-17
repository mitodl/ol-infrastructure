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
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ol_infrastructure.infrastructure.grafana_alerting.dashboards.datasources import (
    MIMIR_DATASOURCE_REF,
)

_DURATION = "http_server_duration_milliseconds"

# RE2's metacharacters. The endpoint patterns these dashboards filter on are
# themselves Django URL regexes (`api/v2/courses/$`, `^api/v0/users/me/$`), so
# dropping them into a PromQL `=~` alternation unescaped silently matches the
# wrong thing rather than erroring: `api/v2/courses/$` read as a regex is an
# anchored match for `api/v2/courses/`, which is not a label value that exists.
_RE2_METACHARACTERS = r"\.+*?()|[]{}^$"


def _promql_regex_escape(literal: str) -> str:
    """Escape a literal so it matches only itself in a PromQL `=~` matcher.

    Two layers of escaping stack here, and getting only the inner one right
    fails loudly while getting neither right fails silently.

    The inner layer is RE2, which needs `^` written as `\\^`. The outer layer is
    PromQL's string literal, which follows Go's rules: `\\^` inside double
    quotes is not a recognised escape sequence, and Mimir rejects the whole
    query with `parse error: unknown escape sequence U+005E '^'`. Each
    backslash therefore has to survive into the regex as a doubled backslash in
    the query text, so a metacharacter `c` is emitted as `\\\\c`.

    Verified against the production stack on 2026-09-17 via /api/ds/query: the
    single-backslash form returns HTTP 400 for every panel using it, the
    doubled form returns all nine of this journey's endpoints.
    """
    return "".join(
        f"\\\\{character}" if character in _RE2_METACHARACTERS else character
        for character in literal
    )


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
    """

    uid: str
    title: str
    slug: str
    description: str
    primary_service: str
    focus_step: str
    steps: list[JourneyStep]
    known_gaps: str = ""
    _services: list[str] = field(default_factory=list, init=False)

    @property
    def services(self) -> list[str]:
        """Distinct services the journey touches, in first-call order."""
        return list(dict.fromkeys(step.service for step in self.steps))

    @property
    def target_regex(self) -> str:
        """PromQL-ready regex alternation matching exactly these endpoints."""
        return "|".join(_promql_regex_escape(step.target) for step in self.steps)

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
    above them, and Grafana silently overlaps panels rather than complaining.
    """

    def __init__(self) -> None:
        self.y = 0

    def row(self) -> int:
        """Reserve a full-width row divider (always 1 unit tall)."""
        y = self.y
        self.y += 1
        return y

    def place(self, *, width: int, height: int, x: int = 0) -> dict[str, int]:
        """Place a panel at the cursor; advance only when the row is full."""
        position = {"h": height, "w": width, "x": x, "y": self.y}
        if x + width >= 24:  # noqa: PLR2004 - Grafana's grid is 24 wide
            self.y += height
        return position


def _rate(
    selector: str, suffix: str = "count", window: str = "$__rate_interval"
) -> str:
    return f"rate({_DURATION}_{suffix}{{{selector}}}[{window}])"


def _quantile(
    quantile: float, selector: str, by: str = "", window: str = "$__rate_interval"
) -> str:
    grouping = f"le, {by}" if by else "le"
    return (
        f"histogram_quantile({quantile}, "
        f"sum by ({grouping}) ({_rate(selector, 'bucket', window)}))"
    )


def _templating(journey: Journey) -> dict[str, Any]:
    """Dashboard variables.

    `steps` and `services` are constants rather than editable textboxes: they
    are the definition of the journey, and a reader who edits them is looking
    at a different journey. Everything a reader legitimately changes --- which
    service, which two releases, which endpoint --- is a visible picker.

    `endpoint` is queried rather than hardcoded so that it follows `service`
    and only ever offers endpoints that have data on the selected stack.
    """
    return {
        "list": [
            {
                "name": "steps",
                "type": "constant",
                "query": journey.target_regex,
                "current": {
                    "text": journey.target_regex,
                    "value": journey.target_regex,
                },
                "hide": 2,
            },
            {
                "name": "services",
                "type": "constant",
                "query": "|".join(journey.services),
                "current": {
                    "text": "|".join(journey.services),
                    "value": "|".join(journey.services),
                },
                "hide": 2,
            },
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
                "query": (
                    f'label_values({_DURATION}_count{{service_name="$service"}}, '
                    "service_version)"
                ),
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
                "query": (
                    f'label_values({_DURATION}_count{{service_name="$service"}}, '
                    "service_version)"
                ),
                "sort": 8,
                "refresh": 2,
            },
            {
                "name": "endpoint",
                "label": "Endpoint",
                "type": "query",
                "datasource": MIMIR_DATASOURCE_REF,
                "query": (
                    f"label_values({_DURATION}_count{{"
                    'service_name="$service", http_target=~"$steps"}, '
                    "http_target)"
                ),
                "current": {
                    "text": journey.focus_step,
                    "value": journey.focus_step,
                },
                "refresh": 2,
            },
        ]
    }


def _comparison_panels(
    journey: Journey,  # noqa: ARG001 - kept for symmetry with the other sections
    layout: _Layout,
    timeseries_panel: Callable[..., dict[str, Any]],
    stat_panel: Callable[..., dict[str, Any]],
    table_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
) -> list[dict[str, Any]]:
    """Before/after for one release pair, by `service_version`."""
    endpoint = 'service_name="$service", http_target="$endpoint"'
    before = f'{endpoint}, service_version="$baseline"'
    after = f'{endpoint}, service_version="$candidate"'
    # $__range rather than $__rate_interval: each release only has samples
    # during the window it was live, so a rate over the dashboard's whole range
    # is the only window that covers both. It deflates the per-second rate for
    # a release that ended mid-range (the increase is spread over the full
    # window) but leaves the quantiles correct, because every histogram bucket
    # is deflated by the same factor.
    journey_before = (
        'service_name="$service", http_target=~"$steps", service_version="$baseline"'
    )
    journey_after = (
        'service_name="$service", http_target=~"$steps", service_version="$candidate"'
    )
    p95_before = _quantile(0.95, journey_before, by="http_target", window="$__range")
    p95_after = _quantile(0.95, journey_after, by="http_target", window="$__range")

    return [
        row_panel(
            title="Release comparison - $service: $baseline vs $candidate",
            y=layout.row(),
        ),
        stat_panel(
            title="p95 before - $endpoint",
            expr=_quantile(0.95, before, window="$__range"),
            grid_pos=layout.place(width=6, height=4, x=0),
            unit="ms",
            decimals=0,
        ),
        stat_panel(
            title="p95 after - $endpoint",
            expr=_quantile(0.95, after, window="$__range"),
            grid_pos=layout.place(width=6, height=4, x=6),
            unit="ms",
            decimals=0,
        ),
        stat_panel(
            title="Change in p95",
            expr=(
                f"(({_quantile(0.95, after, window='$__range')}) - "
                f"({_quantile(0.95, before, window='$__range')})) / "
                f"({_quantile(0.95, before, window='$__range')})"
            ),
            grid_pos=layout.place(width=6, height=4, x=12),
            unit="percentunit",
            decimals=1,
        ),
        stat_panel(
            title="Requests compared",
            expr=(
                f"sum(increase({_DURATION}_count{{{before}}}[$__range])) + "
                f"sum(increase({_DURATION}_count{{{after}}}[$__range]))"
            ),
            grid_pos=layout.place(width=6, height=4, x=18),
            unit="short",
            decimals=0,
        ),
        timeseries_panel(
            title="$endpoint latency by release",
            queries=[
                {
                    "expr": _quantile(
                        quantile,
                        'service_name="$service", http_target="$endpoint"',
                        by="service_version",
                    ),
                    "legend_format": f"p{int(quantile * 100)} {{{{service_version}}}}",
                }
                for quantile in (0.50, 0.95, 0.99)
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
            expr=(f"sum by (service_version) ({_rate(endpoint)})"),
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
            title="Per-endpoint before/after across the whole journey",
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
                        f"(increase({_DURATION}_count{{{journey_after}}}[$__range]))"
                    ),
                    "title": "Requests after",
                    "unit": "short",
                },
            ],
            grid_pos=layout.place(width=24, height=10, x=0),
            description=(
                "Whether the fix moved the endpoint it targeted, and whether "
                "it moved anything else. The change columns subtract two "
                "vectors matched on http_target, so an endpoint served by only "
                "one of the two releases is dropped from the row set rather "
                "than shown against a zero. Set the dashboard time range to "
                "span both releases; each column aggregates over that whole "
                "range and reads each release's own window out of it."
            ),
        ),
    ]


def _trace_panels(
    layout: _Layout,
    traceql_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
) -> list[dict[str, Any]]:
    """Database work per request, from trace structure.

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
    request_spans = '{resource.service.name="$service" && name="GET $endpoint"}'
    postgres_under_request = f'{request_spans} >> {{span.db.system="postgresql"}}'
    sampling_caveat = (
        "Traces are tail-sampled, so both panels describe the sampled "
        "fraction rather than all traffic, and the sampler favours slow "
        "requests, which are also the ones issuing the most queries. Take the "
        "ratio's change between releases, not its absolute value."
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
                "queries per request; a fix that makes query count flat in "
                "result-set size drops that ratio by an order of magnitude "
                "while the panel on the right holds steady. Measured this way "
                "on 2026-09-17, api/v2/courses/$ was running roughly 500 "
                "Postgres queries per request on 1.166.3 through 1.166.5. "
                f"{sampling_caveat}"
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
    layout: _Layout,
    timeseries_panel: Callable[..., dict[str, Any]],
    table_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
) -> list[dict[str, Any]]:
    """Show the journey as it stands now, independent of any release pair."""
    journey = 'service_name=~"$services", http_target=~"$steps"'
    errors_5xx = f'{journey}, http_status_code=~"5.."'
    errors_4xx = f'{journey}, http_status_code=~"4.."'
    return [
        row_panel(title="Journey overview - all steps, all releases", y=layout.row()),
        table_panel(
            title="Per-step RED",
            join_field="http_target",
            sort_by="p95 (ms)",
            columns=[
                {
                    "expr": f"sum by (http_target) ({_rate(journey)})",
                    "title": "Requests/sec",
                    "unit": "reqps",
                },
                {
                    "expr": _quantile(0.95, journey, by="http_target"),
                    "title": "p95 (ms)",
                    "unit": "ms",
                },
                {
                    "expr": _quantile(0.99, journey, by="http_target"),
                    "title": "p99 (ms)",
                    "unit": "ms",
                },
                {
                    # Deliberately no `or vector(0)`: vector(0) carries no
                    # labels, so it can never match a `by (http_target)`
                    # denominator. A step with no 5xx leaves the cell empty,
                    # which is honest, rather than joining onto every row.
                    "expr": (
                        f"sum by (http_target) ({_rate(errors_5xx)}) / "
                        f"sum by (http_target) ({_rate(journey)})"
                    ),
                    "title": "5xx ratio",
                    "unit": "percentunit",
                },
            ],
            grid_pos=layout.place(width=24, height=9, x=0),
            description=(
                "One row per journey step. These are whole-service figures for "
                "each endpoint, not only the requests this page made: nothing "
                "on the metric distinguishes a call from the organization "
                "dashboard from the same endpoint called elsewhere. For steps "
                "that only this journey calls the two are the same thing."
            ),
        ),
        timeseries_panel(
            title="p95 by step",
            expr=_quantile(0.95, journey, by="http_target"),
            legend_format="{{http_target}}",
            grid_pos=layout.place(width=12, height=8, x=0),
            unit="ms",
            legend_calc="max",
        ),
        timeseries_panel(
            title="Request rate by step",
            expr=f"sum by (http_target) ({_rate(journey)})",
            legend_format="{{http_target}}",
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
                    "expr": f"sum by (http_target) ({_rate(errors_5xx)})",
                    "legend_format": "5xx {{http_target}}",
                },
                {
                    "expr": f"sum by (http_target) ({_rate(errors_4xx)})",
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
        # Wide by default: a release comparison needs a window containing both
        # releases, and our deploy cadence puts that at a day or more.
        "time": {"from": "now-2d", "to": "now"},
        "refresh": "5m",
        "templating": _templating(journey),
        "panels": [
            *_comparison_panels(
                journey, layout, timeseries_panel, stat_panel, table_panel, row_panel
            ),
            *_trace_panels(layout, traceql_panel, row_panel),
            *_overview_panels(layout, timeseries_panel, table_panel, row_panel),
        ],
    }


def create(
    folder_uid: Any,
    journeys: list[Journey],
    timeseries_panel: Callable[..., dict[str, Any]],
    stat_panel: Callable[..., dict[str, Any]],
    table_panel: Callable[..., dict[str, Any]],
    traceql_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
    create_dashboard: Callable[..., None],
    resource_opts: Any,
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
