"""PromQL fragments shared by the HTTP-server dashboards.

Both service_red and user_journey read the same OTel metric family, so the
rate/quantile builders live here rather than once per module -- a correction to
the histogram grouping should not have to be made twice and land in one.

WHY `http_target`: the wsgi/asgi instrumentation carries the Django URL
*pattern* on the `http.target` metric attribute and emits no `http.route`, so
`http_target` is the endpoint dimension for these services. See service_red.py
for the measurements behind that.
"""

DURATION_METRIC = "http_server_duration_milliseconds"

# `sum(...) or vector(0)` keeps a ratio panel reading 0 rather than "No data"
# during the (common, desirable) windows with no errors at all -- without it the
# numerator is an empty vector and the whole division returns nothing. Only
# valid where the surrounding aggregation is label-less: `vector(0)` carries no
# labels, so it can never match a `by (...)` denominator.
ZERO = "or vector(0)"


def rate(selector: str, suffix: str = "count", window: str = "$__rate_interval") -> str:
    """Per-second rate of one of the duration family's series.

    :param selector: Label matchers, without the enclosing braces.
    :param suffix: `count`, `sum` or `bucket`.
    :param window: Range-vector window, usually a Grafana interval macro.
    """
    return f"rate({DURATION_METRIC}_{suffix}{{{selector}}}[{window}])"


def quantile(
    quantile_value: float,
    selector: str,
    by: str = "",
    window: str = "$__rate_interval",
) -> str:
    """Latency quantile interpolated from the duration histogram.

    `le` is always in the grouping because `histogram_quantile` reads the
    bucket boundary from it; `by` adds the dimensions the panel legend or a
    table join needs on top of that.

    :param quantile_value: Quantile as a fraction, e.g. 0.95.
    :param selector: Label matchers, without the enclosing braces.
    :param by: Extra grouping labels, comma-separated, or "" for none.
    :param window: Range-vector window, usually a Grafana interval macro.
    """
    grouping = f"le, {by}" if by else "le"
    return (
        f"histogram_quantile({quantile_value}, "
        f"sum by ({grouping}) ({rate(selector, 'bucket', window)}))"
    )
