"""App-side latency and error-rate alerting on http.server.duration.

Fills the gap documented while moving RED dashboards off trace-derived
spanmetrics (see dashboards/service_red.py): dumping all 127 Grafana-managed
rules in the production stack (2026-09-01) found no rule referencing
`http_server_duration_milliseconds_*`, and the only latency alerting anywhere
was three Synthetic Monitoring probe rules -- one URL polled externally, not
the service's own view of its traffic. HTTP error alerting existed but was
indirect: `log_rules/mit_learn.py` parses the nginx sidecar access log per
namespace, and `metric_rules/apisix_edge.py` watches the edge. Neither is the
application's own signal, and neither covers ocw-studio, odl-video-service, or
learn-ai as those onboard onto OTel metrics.

`http_server_duration_milliseconds_*` is live for learn-webapp,
mitxonline-webapp and learn-ai-webapp in grafanacloud-prom, bypasses the tail
sampler (only traces route through the sampling collector, not metrics -- see
service_red.py), and covers all traffic rather than the sampled fraction that
made trace-derived RED unusable for alerting in the first place.

Grouped by `service_name`, not per-endpoint
--------------------------------------------
Per-endpoint spread is wide by design: measured 2026-09-01,
`^checkout/result/` ran a 9.7s p95 and `^api/v1/webhooks/ovs_videos/$` ran
7.1s, against a whole-service p95 of 380ms. A rule scoped to `http_target`
would either need a threshold high enough to never fire on those legitimately
slow endpoints (defeating the point) or it would page constantly on them. A
whole-service p95 already answers a different, useful question -- "is
customer-facing latency degraded" -- without a rare slow endpoint dominating
it, because a percentile only moves when a large share of *all* requests
cross the line.

Health-check exclusion
-----------------------
`http_target=~"^health.*"` (readiness/startup probes) is 9% of mitxonline's
traffic and is fast, so it dilutes the whole-service p95 downward and would
mask a real regression in the traffic that matters. Excluded from both the
latency and error-rate selectors below -- probes fail differently than user
traffic and would otherwise contribute noise to either signal.

Thresholds, from live data rather than convention
----------------------------------------------------
Baseline p95 measured 2026-09-01: mitxonline-webapp ~340-380ms, learn-webapp
~377-412ms, learn-ai-webapp ~7ms (new, low-volume). Re-measured over a 6-day
window (2026-08-28 -> 2026-09-03, 10m buckets, 30m step) to catch what normal
variance actually looks like rather than a single snapshot: mitxonline-webapp
ranged 245-575ms across the window, with two isolated 30-60min bursts to
900-1130ms early in the window (rollout warm-up, not sustained). learn-webapp
stayed under 460ms throughout.

  Fast/critical: p95 > 2000ms, 5m window, confirmed 5m.  ~5x the highest
                 sustained baseline. Catches an outage-shaped cliff fast.
  Slow/warning:  p95 > 800ms, 10m window, confirmed 30m. ~1.4x the one
                 observed 6-day outlier burst, ~2x normal baseline. The 30m
                 confirmation means an isolated hour-long burst like the one
                 in the calibration window would trip this once, which is the
                 intended catch, not noise -- see the apisix_edge.py "Slow"
                 tier for the same reasoning applied to error rate.

Error ratio ran well under 0.01% across both services in the same window (0%
for learn-webapp, 0.0037% for mitxonline-webapp over 6h at measurement time),
so apisix_edge.py's 5%/1% thresholds (calibrated against an edge host that
spent weeks at 18-25%) would be far too loose here to catch anything short of
a near-total outage.

  Fast/critical: 5xx ratio > 2%, 10m window, confirmed 5m.  >500x baseline.
  Slow/warning:  5xx ratio > 0.5%, 6h window, confirmed 30m. >100x baseline,
                 over a long enough window that a brief blip can't trip it.

Minimum-traffic gate
---------------------
Same purpose as apisix_edge.py's `_MIN_RATE`: without it, a quiet service
turns a two-request blip into a 100% ratio. `_MIN_RATE` (0.05 req/s, ~3
req/min) sits below learn-ai-webapp's current ~1.37 req/s -- so it is covered,
not excluded -- while still filtering out a service between deploys or a
namespace with no real traffic. The gate is written as the left operand of
`and` in every expression, matching the idiom documented at apisix_edge.py's
"Gate clause first" and eks_general.py:108-116.

exec_err_state on the critical tier
------------------------------------
`OTelServiceREDLatencyFast` and `OTelServiceREDErrorRateFast` (the
`severity=critical` rules) use `exec_err_state="KeepLast"`, not `"OK"`,
matching base.py's documented standard: a datasource blip should hold the
rule's last known state, not resolve it silently through what may be an
ongoing incident. The two `severity=warning` rules keep `"OK"` -- going
silent during a transient error is an acceptable trade at that tier.

Routing: Slack, not Rootly, until calibrated
----------------------------------------------
These rules have no firing history yet. Following apisix_edge.py's
calibration pattern: ship carrying `channel=devops-warnings` so they route to
#devops-warnings and deliver zero pages while a real firing rate is
established, rather than either `oblivion` (invisible) or an immediate
`severity` route (which reaches Rootly at the same tier as every paging
alert). Promote to paging the same way apisix_edge.py documents: empty
`_PRODUCTION_ROUTING` once the calibration window closes and the firing rate
reflects real incidents.

Measure with:
  sum by (ruleTitle, labels_service_name) (count_over_time(
    {from="state-history"} | json | current="Alerting"
    | ruleTitle=~`OTelServiceRED.*` [14d]))
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

from ol_infrastructure.lib.pulumi_helper import parse_stack

_DURATION = "http_server_duration_milliseconds"

# Excludes readiness/startup probe traffic from both latency and error-rate
# selectors. See the module docstring's "Health-check exclusion" section.
# `http_target` carries the Django URL pattern verbatim, and a `re_path()`
# route's pattern includes a literal leading `^` character (see
# dashboards/service_red.py:46-50); a `path()` route's does not. A plain
# `^health.*` PromQL anchor only matches the latter form -- against the
# former, `!~"^health.*"` never matches, so the health traffic it's meant to
# exclude just as never gets excluded. `\\^?` (a backslash-escaped, optional
# literal caret) matches both route-registration styles.
_SELECTOR = 'service_name=~".+", http_target!~"^\\^?health.*"'
_5XX_SELECTOR = f'{_SELECTOR}, http_status_code=~"5.."'

# Requests/sec a service must sustain over the same window as the ratio/quantile
# before its value is treated as meaningful. See the module docstring.
_MIN_RATE = "0.05"

_SLACK_CHANNEL = "devops-warnings"

# Routing labels for the production stack, on top of each rule's own `severity`.
# Emptying this dict is the whole of promoting these rules to paging -- see the
# module docstring and apisix_edge.py's calibration section for the pattern.
_PRODUCTION_ROUTING = {"channel": _SLACK_CHANNEL}


def _rate_gate(window: str) -> str:
    return f"sum by (service_name) (rate({_DURATION}_count{{{_SELECTOR}}}[{window}])) > {_MIN_RATE}"


def _latency_expr(window: str, threshold_ms: str) -> str:
    """Build a gated whole-service p95-latency expression for a single window."""
    return (
        f"{_rate_gate(window)}"
        " and "
        f"histogram_quantile(0.95, sum by (le, service_name) "
        f"(rate({_DURATION}_bucket{{{_SELECTOR}}}[{window}])))"
        f" > {threshold_ms}"
    )


def _error_ratio_expr(window: str, threshold: str) -> str:
    """Build a gated 5xx-ratio expression for a single window."""
    return (
        f"{_rate_gate(window)}"
        " and "
        f"sum by (service_name) (rate({_DURATION}_count{{{_5XX_SELECTOR}}}[{window}]))"
        f" / sum by (service_name) (rate({_DURATION}_count{{{_SELECTOR}}}[{window}]))"
        f" > {threshold}"
    )


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create app-side RED alert rule groups for OTel-instrumented services."""
    if parse_stack().env_suffix == "production":
        routing = _PRODUCTION_ROUTING
    else:
        # CI and QA are non-production in their entirety. Pinned here rather
        # than read from `_PRODUCTION_ROUTING` so that promoting production
        # leaves these stacks on Slack. See apisix_edge.py's same pattern.
        routing = {"channel": _SLACK_CHANNEL}

    alerting.RuleGroup(
        "otel-service-red-latency",
        name="otel-service-red-latency",
        folder_uid=folder_uid,
        interval_seconds=60,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="OTelServiceREDLatencyFast",
                condition="C",
                for_="5m",
                no_data_state="OK",
                # KeepLast, not OK, on the critical tier: see base.py's
                # exec_err_state docstring -- a datasource blip should hold
                # the rule's last known state, not resolve it silently.
                exec_err_state="KeepLast",
                labels={"severity": "critical", **routing},
                annotations={
                    "summary": "{{ $labels.service_name }} p95 latency is over 2000ms",
                    "description": "{{ $labels.service_name }}'s whole-service p95 latency (http.server.duration, health-check traffic excluded) has been over 2000ms for at least 5 minutes. This is the app's own view of latency, not a single-URL blackbox probe, and it bypasses the tail sampler that made trace-derived latency unreliable for alerting.",
                },
                datas=rd(_latency_expr("5m", "2000")),
            ),
            alerting.RuleGroupRuleArgs(
                name="OTelServiceREDLatencySlow",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", **routing},
                annotations={
                    "summary": "{{ $labels.service_name }} p95 latency has been elevated for a sustained period",
                    "description": "{{ $labels.service_name }}'s whole-service p95 latency (http.server.duration, health-check traffic excluded) has been over 800ms for at least 30 minutes. Baseline is 250-450ms; this catches a sustained creep or a burst too short for the Fast rule's 2000ms line but long enough to matter.",
                },
                datas=rd(_latency_expr("10m", "800")),
            ),
        ],
        opts=resource_opts,
    )

    alerting.RuleGroup(
        "otel-service-red-error-rate",
        name="otel-service-red-error-rate",
        folder_uid=folder_uid,
        interval_seconds=60,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="OTelServiceREDErrorRateFast",
                condition="C",
                for_="5m",
                no_data_state="OK",
                # KeepLast, not OK, on the critical tier: see base.py's
                # exec_err_state docstring -- a datasource blip should hold
                # the rule's last known state, not resolve it silently.
                exec_err_state="KeepLast",
                labels={"severity": "critical", **routing},
                annotations={
                    "summary": "{{ $labels.service_name }} is returning over 2% 5xx",
                    "description": "More than 2% of {{ $labels.service_name }}'s requests (health-check traffic excluded) returned a 5xx status over the last 10 minutes, measured from the app's own MeterProvider rather than the APISIX edge or a sampled trace fraction. Baseline measured 2026-09-03 was under 0.01% for both instrumented services.",
                },
                datas=rd(_error_ratio_expr("10m", "0.02")),
            ),
            alerting.RuleGroupRuleArgs(
                name="OTelServiceREDErrorRateSlow",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", **routing},
                annotations={
                    "summary": "{{ $labels.service_name }} has had an elevated 5xx rate for hours",
                    "description": "More than 0.5% of {{ $labels.service_name }}'s requests (health-check traffic excluded) returned a 5xx status over the last 6 hours, measured from the app's own MeterProvider. Catches a slow error-rate creep too gradual for the Fast rule's 10m window, the same failure shape apisix_edge.py's Slow tier catches at the edge.",
                },
                datas=rd(_error_ratio_expr("6h", "0.005")),
            ),
        ],
        opts=resource_opts,
    )
