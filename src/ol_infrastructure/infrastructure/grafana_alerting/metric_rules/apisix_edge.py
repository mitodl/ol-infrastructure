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
