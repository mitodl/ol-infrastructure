"""APISIX edge 5xx rate alert rules.

APISIX fronts every public host, and `apisix_http_status{matched_host, code}`
gives a clean per-host success/failure ratio at the edge. Until these rules the
only HTTP error alerting was two Loki rules parsing the nginx sidecar log in two
namespaces, which measures one service's view rather than what the internet sees.

Why the edge and not the app: measured 2026-07-08 -> 2026-08-07,
api.mitxonline.mit.edu went from 0% to a sustained 18-25% 5xx rate at the edge
over four weeks with zero alerts. The rule meant to catch it
(log_rules/mit_learn.py::mitxonline-5xx-error-percentage) parses the mitxonline
nginx sidecar and needs >5% of *all* that namespace's traffic; the failures were
a retry loop against one endpoint, which never moved a whole-namespace ratio.

Two windows, because the two failure shapes need different ones:
  fast — a cliff. 5% over 10m, confirmed for 5m. Catches an outage in ~15 min.
  slow — a creep. 1% over 6h, confirmed for 30m. This is the one that would have
         caught api.mitxonline in week one, when it first crossed 5.5%.

Minimum-traffic gate
--------------------
A bare ratio fires forever on idle hosts. Measured 2026-08-07:
courses-backend.learn.mit.edu showed a 33.3% 5xx rate on *three requests a day*
(one 500), and courses-backend.rc.learn.mit.edu independently showed 21.9% on
~32/day on the QA stack. Both are arithmetic on a tiny denominator, not outages.

`_MIN_RATE` (0.01 req/s) separates them from the hosts that matter with room to
spare -- at the time of writing courses-backend.learn ran 0.000035 req/s and
api.mitxonline.mit.edu, the host these rules exist to catch, ran 0.083 req/s.
That is a ~288x margin below and ~8x above. Note the gate is deliberately
absolute, not a percentile: it answers "is anyone actually using this host",
which is what makes a ratio meaningful.

Gate clause first
-----------------
The gate is written as the LEFT operand of `and` in every expression. PromQL's
`and` carries through the value of its left-hand side, and base.py's `_rule_data`
feeds that into a threshold stage firing on `last(A) > 0`. A ratio on the left
would also work numerically here (it is > 0 whenever it exceeds the threshold),
but the gate on the left is the safer idiom and matches the reasoning already
documented at eks_general.py:108-116 -- put the clause whose value you want to
survive on the left, every time, rather than reasoning case by case.

Calibration: these rules carry NO severity label
------------------------------------------------
Deliberate, and the reason is in alertmanager.py's route tree: the default
receiver is `oblivion`, so an alert with no `severity` is evaluated and recorded
but delivered nowhere. That gives a full firing history in
`grafanacloud-alert-state-history` -- the same source used to measure every claim
above -- with zero paging risk while the thresholds are still guesses.

Promote by adding `labels={"severity": "warning"}` (then "critical") once the
history shows what they actually catch. Do not promote before checking
api.learn.mit.edu: it sat at 1.09% when these were written, just above the slow
rule's 1% line, and needs a deliberate decision (raise the threshold to 2%, or
accept it as the genuine signal its 304,860 absolute 502s/30d suggest).

Adding that label is the *only* change promotion needs, but only because
`matched_host` was added to `NotificationPolicy.group_bies` in alertmanager.py
alongside these rules. These aggregate `sum by (matched_host)`, so that is the
one resource-identifying label they carry; without it in the grouping list every
firing host would collapse into a single notification group per rule and one
host changing state would resend all the others. If you add a rule here that
groups by something else, add that label there too.

Measure with:
  sum by (ruleTitle) (count_over_time(
    {from="state-history"} | json | current =~ `Alerting.*` [14d]))
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

# Requests/sec a host must sustain over the same window as the ratio before its
# error ratio is treated as meaningful. See the module docstring for the measured
# values this sits between.
_MIN_RATE = "0.01"


def _error_ratio_expr(window: str, threshold: str) -> str:
    """Build a gated 5xx-ratio expression for a single window.

    Returns series only for hosts that both carry real traffic and exceed the
    error threshold, so the rule fires per `matched_host`.
    """
    return (
        f"sum by (matched_host) (rate(apisix_http_status[{window}])) > {_MIN_RATE}"
        " and "
        f'sum by (matched_host) (rate(apisix_http_status{{code=~"5.."}}[{window}]))'
        f" / sum by (matched_host) (rate(apisix_http_status[{window}]))"
        f" > {threshold}"
    )


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create APISIX edge 5xx rate alert rule groups."""
    alerting.RuleGroup(
        "apisix-edge-error-rate",
        name="apisix-edge-error-rate",
        folder_uid=folder_uid,
        interval_seconds=60,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="APISIXEdge5xxRateFast",
                condition="C",
                for_="5m",
                no_data_state="OK",
                exec_err_state="OK",
                # No severity label: routes to `oblivion` while calibrating.
                # See the module docstring.
                labels={},
                annotations={
                    "summary": "{{ $labels.matched_host }} is returning over 5% 5xx at the APISIX edge",
                    "description": "More than 5% of requests to {{ $labels.matched_host }} returned a 5xx status at the APISIX edge over the last 10 minutes. This measures what clients actually receive, not one service's own view of itself.",
                },
                datas=rd(_error_ratio_expr("10m", "0.05")),
            ),
            alerting.RuleGroupRuleArgs(
                name="APISIXEdge5xxRateSlow",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={},
                annotations={
                    "summary": "{{ $labels.matched_host }} has been returning over 1% 5xx for hours",
                    "description": "More than 1% of requests to {{ $labels.matched_host }} returned a 5xx status at the APISIX edge over the last 6 hours. This catches a slow error-rate creep that a short-window threshold cannot: api.mitxonline.mit.edu climbed from 0% to 25% over four weeks in July 2026 without tripping any existing rule.",
                },
                datas=rd(_error_ratio_expr("6h", "0.01")),
            ),
        ],
        opts=resource_opts,
    )
