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

The gate is a floor on *sustained* traffic, not a guarantee against small
denominators. Re-measured 2026-08-24 over the 14-day calibration window,
courses-backend.learn.mit.edu peaks at 0.897 req/s in its busiest 10m while
sitting at 0.0016 req/s on median: a bursty host opens the gate during a burst,
and a handful of 5xx inside that burst still trips the ratio. Its 14 Fast
firings are that, not a broken gate -- and with 674 real 5xx over the window
they are not false either. Raising `_MIN_RATE` to suppress them would also
raise it past api.mitxonline.mit.edu (0.054 req/s), the host these rules exist
for, so the gate stays where it is.

Gate clause first
-----------------
The gate is written as the LEFT operand of `and` in every expression. PromQL's
`and` carries through the value of its left-hand side, and base.py's `_rule_data`
feeds that into a threshold stage firing on `last(A) > 0`. A ratio on the left
would also work numerically here (it is > 0 whenever it exceeds the threshold),
but the gate on the left is the safer idiom and matches the reasoning already
documented at eks_general.py:108-116 -- put the clause whose value you want to
survive on the left, every time, rather than reasoning case by case.

Calibration is over: these rules deliver to Slack, not Rootly
-------------------------------------------------------------
They shipped 2026-08-10 carrying no labels at all, which routed them to
alertmanager.py's default `oblivion` receiver -- evaluated and recorded, but
delivered nowhere -- so that a firing history could be built with zero paging
risk while the thresholds were still guesses. That window closed 2026-08-24 and
the history was measured (`grafanacloud-alert-state-history`, 14d):

  Fast  191 firings, 13.6/day. api.mitxonline.mit.edu alone is 139 of them (73%),
        all one known-open defect (ol-django#538). Then studio.courses.learn 20,
        courses-backend.learn 14, analytics.learn 4, opik 4, studio-staging 4,
        courses.xpro 2, studio.mitx 2, nb.learn 1, staging.mitx 1.
  Slow  23 firings, 1.6/day. studio.courses.learn 10, courses-backend.learn 3,
        studio-staging 3, api.mitxonline 2, opik 2, studio.mitx 2, analytics 1.

`severity` alone would not have been a safe promotion. In alertmanager.py's
route tree `warning` and `critical` terminate at the *same* `rootly` contact
point, so labelling these `severity=warning` sends 15 pages/day to the
production on-call -- into a remediation effort whose whole premise is that the
current ~48/day is too many, and with 73% of it one already-tracked defect.

So they carry `channel=devops-warnings` as well, which alertmanager.py routes to
the #devops-warnings Slack channel and terminates before either severity route
is reached. That is a strict improvement on `oblivion` (the signal is now
visible to a human) at zero paging cost. `severity` still rides along and picks
the Slack formatting: Fast is a cliff, so `critical` (red :alert:); Slow is a
creep, so `warning` (yellow).

Promote to paging by emptying `_PRODUCTION_ROUTING` below -- the rules then fall
through to the `severity` routes and reach Rootly. Do that once
api.mitxonline.mit.edu (ol-django#538) and studio.courses.learn.mit.edu are
fixed and the baseline firing rate reflects real incidents rather than two
known-broken hosts. Re-measure first, with the query at the bottom of this
docstring.

One promotion caveat from the original calibration note is now resolved:
api.learn.mit.edu sat at 1.09% when these rules were written, just above the
slow rule's 1% line, and was flagged as needing a deliberate threshold decision.
Re-measured 2026-08-24 it runs 0.005% 5xx on 53 req/s -- the busiest host at the
edge -- and fired neither rule once in 14 days. No threshold change needed.

Every host in the production stack is production
------------------------------------------------
Worth stating because the hostnames suggest otherwise: `staging.mitx.mit.edu`
and its `*-staging.mitx.mit.edu` siblings are NOT a non-production tier of
mitx. `mitx-staging` is a peer deployment of `mitx` with its own VPC and its own
CI/QA/Production stacks (infrastructure/aws/network/__main__.py:157,
`Pulumi.mitx-staging.Production.yaml`), serving residential course authors who
write courses as XML and push to GitHub instead of using the Studio UI. Its
firings above are production signal and are treated as such -- do not add a
hostname filter to exclude them.

So there is no host class to split on here, and the environment boundary is the
stack boundary alone. This module is deployed to every stack, each pointing at
its own Grafana Cloud stack and Mimir tenant; the CI and QA stacks are
non-production in their entirety and pin `channel` separately in `create()` so
that promoting production cannot reach them. That is also the only split that
would be reliable: QA host naming is far too irregular to match (`.rc.`, `-rc.`,
`.qa.`, `-qa.`, `-qa-draft.`, and a bare leading `rc.mitxonline.mit.edu` all
coexist), and a regex that silently missed one would page the on-call for a QA
host. `parse_stack()` already knows the answer, so ask it instead of inferring.
Same pattern as synthetic_monitoring.py:357.

Measure with:
  sum by (ruleTitle, labels_matched_host) (count_over_time(
    {from="state-history"} | json | current="Alerting"
    | ruleTitle=~`APISIXEdge5xx.*` [14d]))

Gateway rate limiting (APISIXEdgeRateLimited)
---------------------------------------------
ol-infrastructure#4759 puts limit-req/limit-conn in front of api.learn.mit.edu,
keyed on client IP and rejecting with 429. If the threshold is wrong, or a
routing change moves an aggregating client (a proxy, the SSR server) onto a
limited route, the result is real users getting 429s, and without this rule that
surfaces as user reports.

The rule matches on `response_source="apisix"`, not on the host. APISIX sets
that label when the gateway itself produced the response, so it separates a
plugin rejection from an app's own throttling passed through from upstream.
That distinction matters: measured 2026-09-28 over 30 days, the production edge
saw ~37.6k 429s on courses.learn.mit.edu, ~10.6k on lms.mitx.mit.edu, 6 on
courses.xpro and 4 on api.learn, every one of them `response_source="upstream"`
(edxapp and Django throttling). Gateway-sourced 429s were zero on every host.
So the baseline is zero and any firing is a limit plugin rejecting traffic.
That holds for any host that later gets a limit plugin, with one condition:
limit-req and limit-conn default `rejected_code` to 503, and #4759 overrides it
to 429. A limit plugin configured some other way shows up as
`code="503", response_source="apisix"` and counts toward the 5xx rules above
instead of this one.

It fires on any rejection rather than a rate, and with no pending period,
because a legitimate client should never reach the limit. #4759 sized it at
~10x the busiest single browser IP over 30 days (~5 req/s against 50), so a
client that trips it is worth a human look at who it is. `increase()` alone
would miss that first rejection: each (pod, route, host) series only exists
once a pod has rejected something, it appears already at 1, and `increase()`
needs two samples. The limits are per pod across the APISIX replicas, so a
short burst is often exactly one rejection per pod. The `unless ... offset`
clause catches those new series.

The metric has no client label, and "which client" is the first question.
Answer it from the access log, where a gateway rejection has `upstream_status=-`:
  sum by (remote_addr, http_user_agent) (count_over_time(
    {service_name="apache-apisix", cluster="applications-production"}
    |= `status=429` | logfmt | status="429" | upstream_status="-" [1h]))

Routed to Slack like the 5xx rules, for the same reason: it has no firing
history yet. The opposite signal (a month with zero firings means the limit can
be tightened) is only visible from this rule's state history, so check it there
before changing the #4759 thresholds.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

from ol_infrastructure.lib.pulumi_helper import parse_stack

# Requests/sec a host must sustain over the same window as the ratio before its
# error ratio is treated as meaningful. See the module docstring for the measured
# values this sits between.
_MIN_RATE = "0.01"

# Route to the #devops-warnings Slack channel rather than Rootly. See
# alertmanager.py's `channel` branch, and the calibration section of the module
# docstring for why this is here instead of a bare `severity`.
_SLACK_CHANNEL = "devops-warnings"

# Routing labels for the production stack, on top of each rule's own `severity`.
# Emptying this dict is the whole of promoting these rules to paging: with no
# `channel` they fall through alertmanager.py's severity routes to Rootly.
# Deliberately separate from the CI/QA branch in `create()`, which pins
# `channel` unconditionally -- so that edit cannot page for a QA host.
_PRODUCTION_ROUTING = {"channel": _SLACK_CHANNEL}


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


# Gateway-produced 429s per host and route. Upstream 429s (app throttling) are
# excluded by `response_source`; see the module docstring.
_GATEWAY_429 = 'apisix_http_status{code="429", response_source="apisix"}'
_RATE_LIMITED_EXPR = (
    f"sum by (matched_host, route) (increase({_GATEWAY_429}[10m])) > 0"
    " or "
    f"count by (matched_host, route) ({_GATEWAY_429} unless {_GATEWAY_429} offset 10m)"
)


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create APISIX edge 5xx rate alert rule groups."""
    if parse_stack().env_suffix == "production":
        routing = _PRODUCTION_ROUTING
    else:
        # CI and QA are non-production in their entirety. Pinned here rather
        # than read from `_PRODUCTION_ROUTING` so that promoting production
        # leaves these stacks on Slack. See the module docstring.
        routing = {"channel": _SLACK_CHANNEL}

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
                # A cliff, not a creep -- `critical` picks the red :alert:
                # Slack formatting. See the module docstring on why `channel`
                # rides along with it.
                labels={"severity": "critical", **routing},
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
                labels={"severity": "warning", **routing},
                annotations={
                    "summary": "{{ $labels.matched_host }} has been returning over 1% 5xx for hours",
                    "description": "More than 1% of requests to {{ $labels.matched_host }} returned a 5xx status at the APISIX edge over the last 6 hours. This catches a slow error-rate creep that a short-window threshold cannot: api.mitxonline.mit.edu climbed from 0% to 25% over four weeks in July 2026 without tripping any existing rule.",
                },
                datas=rd(_error_ratio_expr("6h", "0.01")),
            ),
        ],
        opts=resource_opts,
    )

    alerting.RuleGroup(
        "apisix-edge-rate-limit",
        name="apisix-edge-rate-limit",
        folder_uid=folder_uid,
        interval_seconds=60,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="APISIXEdgeRateLimited",
                condition="C",
                # Any rejection is the signal; see the module docstring.
                for_="0s",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", **routing},
                annotations={
                    "summary": "APISIX is rate limiting clients on {{ $labels.matched_host }}",
                    "description": 'The APISIX gateway itself returned 429 to requests for {{ $labels.matched_host }} (route {{ $labels.route }}) over the last 10 minutes. The limit sits ~10x above the busiest browser seen over 30 days, so either one client is misbehaving or the limit is catching legitimate traffic. Find the client in the APISIX access log: gateway rejections have status=429 and upstream_status=-, e.g. {service_name="apache-apisix"} |= `status=429` | logfmt | status="429" | upstream_status="-" | host="{{ $labels.matched_host }}", then group by remote_addr.',
                },
                datas=rd(_RATE_LIMITED_EXPR),
            ),
        ],
        opts=resource_opts,
    )
