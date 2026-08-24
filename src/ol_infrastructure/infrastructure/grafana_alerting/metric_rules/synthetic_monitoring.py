"""Grafana Synthetic Monitoring alert rules for MIT Learn.

Three hand-made rules lived in the Synthetic Monitoring folder since April-June
2026, created through the UI and unmanaged. Imported here 2026-08-13 after the
NextJS one paged twice in an hour for a single failed probe. They are grouped in
one module because they are the same rule three times over, differing only in
which endpoint they probe.

Each check now carries three rules: two on availability (`probe_success`, a
cliff window and a creep window) and one on latency (`probe_all_duration_*`,
added 2026-08-24). The first two answer "is it up", the third "is it fast" --
see "Reclaiming the latency signal" below for why the latter needed adding
when the plugin already ships a rule for it.

Not to be confused with the `sm-*` rules in the same folder
(ProbeFailedExecutionsTooHigh, HTTPRequestDurationTooHighAvg,
TLSTargetCertificateCloseToExpiring). Those are generated and owned by the
grafana-synthetic-monitoring-app plugin. Leave them alone -- Pulumi does not
manage them and adopting them would fight the plugin. The latency rules added
here deliberately sit *alongside* HTTPRequestDurationTooHighAvg rather than
replacing it; see "Why a new rule rather than adopting the plugin's" below.

Folder, and why this is production-only
---------------------------------------
`_SM_FOLDER_UID` is the plugin's own folder, not a Pulumi-created one, so this
module takes no `folder_uid` parameter (unlike every other metric_rules
sub-module). The rules must stay in that folder: the folder UID is half the
import identity, so moving them to "Infrastructure Alerts" would destroy and
recreate all three, losing their alert-state history and any live silences.

That folder UID only exists on the production Grafana stack. QA has no Synthetic
Monitoring folder at all, and CI's is `ffqmgh1ukxam8a` (plus a legacy
`6GJToXwnz`) -- the UID is per-stack, unlike the `grafanacloud-prom` datasource
UID that is deliberately uniform everywhere. Registering these outside
production would fail against a folder that is not there, and the pipeline
deploys CI -> QA -> Production, so it would break at the first stage.

The endpoints are production hosts regardless (learn.mit.edu,
api.learn.mit.edu, next.learn.mit.edu), and the Synthetic Monitoring checks
feeding `probe_success` are only configured on the production stack, so there is
nothing for these rules to evaluate elsewhere. Hence the early return below,
matching how `__main__.py` gates `pingdom_checks` on the same condition.

Why the expression is inverted
------------------------------
The obvious translation of "fire when availability drops" is

    avg_over_time(probe_success{...}[5m]) < 0.8      # WRONG

and it is silently broken. base.py's `_rule_data` feeds stage A's value into a
threshold stage that fires on `last(A) > 0`. When every probe in the window
fails, `avg_over_time` is 0, the `< 0.8` comparison passes, and stage A returns
a series whose *value* is 0 -- which stage C then reads as "not firing". That
form alerts on a partial outage and goes silent on a total one, the exact
inverse of what it is for.

So these measure the failure ratio instead, whose value rises as things get
worse and is positive whenever the rule should fire:

    1 - avg_over_time(probe_success{...}[5m]) > 0.3

This is the same "put the clause whose value you want to survive on the left"
reasoning documented at eks_general.py:108-116 and apisix_edge.py, applied to a
comparison rather than an `and`.

Two windows, because the two failure shapes need different ones
---------------------------------------------------------------
  fast — a cliff. 2+ of the last 5 probes failed, held for 5m. An outage.
  slow — a creep. >4% of the last hour's probes failed, held for 30m.

The single-window rules these replace used `< 1`: *any* one failed probe out of
five, from a single probe location, with no quorum. That is what produced the
paging -- observed firing at A=0.75 and A=0.8, i.e. 3/4 and 4/5 probes
succeeding.

docs/plans/grafana-alerting-remediation-spec.md proposed `< 0.6` (3+ of 5) as
the fix. Both that and the looser 2-of-5 chosen here are necessary but not
sufficient, and the 2026-08-13 Next.js incident is why: a deploy put a
jsdom-backed HTML parse on every server-rendered page, the origin spent two
hours intermittently timing out at the check's 15s ceiling, and *no* 5m-window
threshold caught it. The worst 5m window reached only 2 of 5 failures and held
it for about two minutes -- short of `for_="5m"` on a 300s interval. Over a 1h
window the same period was unmistakable: a flat 0 for the preceding ten hours,
then a climb through 3%, 5%, 7% to 10%.

So the fast window alone is quieter than the rule it replaces but blind to
exactly the degradation that prompted the rewrite. The slow window is what
actually catches that shape.

The slow rules ship unrouted
----------------------------
They carry no `severity` label, so alertmanager.py's default route drops them in
`oblivion`: evaluated and recorded in `grafanacloud-alert-state-history`, but
delivered nowhere. This is the same calibration mechanism apisix_edge.py uses,
and it is here because measurement over the 7 days to 2026-08-13 says both
thresholds are still guesses:

  fast `> 0.3`, for 5m   — would not have fired once in 7 days, on any of the
                           three checks. Not even for the moment on 2026-08-10
                           when all 5 of the Next.js probes in a window failed:
                           it did not stay true across two consecutive 300s
                           evaluations. Safe (it is strictly quieter than the
                           `< 1` rule it replaces, which fired twice in one hour
                           on 2026-08-13) but likely too quiet.
  slow `> 0.04`, for 30m — would have fired ~5 times in 7 days on the API health
                           endpoint alone, whose 1h failure ratio peaks at 8.3%
                           in normal operation. Routing that at `critical` today
                           would recreate the paging problem this rewrite is
                           meant to end.

Per-check baselines, 7d max of the 1h failure ratio, measured 2026-08-13:
learn.mit.edu 1.7%, api.learn.mit.edu/learn/health 8.3%, next.learn.mit.edu
21.7%. The homepage is the only one quiet enough for 4% as written; the other
two need either a per-check threshold or a higher common one.

Promote a slow rule by adding `severity` to its labels and nothing else --
`instance` is already in NotificationPolicy.group_bies. Measure first with:

  sum by (ruleTitle) (count_over_time(
    {from="state-history"} | json | current =~ `Alerting.*` [14d]))

Thresholds sit *between* quantisation steps on purpose
------------------------------------------------------
Probes run every 60s, so a 5m window is quantised to fifths (0.2, 0.4, ...) and
a 1h window to sixtieths (0.0167, 0.0333, 0.05, ...). Both thresholds are placed
in the gaps rather than on a step:

  fast `> 0.3`  -- between 1 failure (0.2) and 2 (0.4)
  slow `> 0.04` -- between 2 failures/hr (0.0333) and 3 (0.05)

A threshold written directly on a step is decided by float representation
rather than by intent: `1 - 0.8` evaluates to 0.19999999999999996, so a `> 0.2`
meaning "more than one failure" happens to work only by accident of rounding.
Sitting in the gap makes the boundary explicit and unambiguous.

Reclaiming the latency signal
----------------------------
The plugin's own `HTTPRequestDurationTooHighAvg [5m]` measures how *slowly*
these three endpoints answer, which nothing else here covers -- the probe
rules above are availability only. It carries no `severity`, so every firing
lands in alertmanager.py's `oblivion`: evaluated, recorded, delivered nowhere.
Measured over the 14 days to 2026-08-24 it is the loudest rule in the estate
outside the Adaptive Traces ML set at 333 Alerting transitions, ~24/day (the
next loudest are APISIXEdge5xxRateFast at 194 and PodOOMKilledCritical at
144), and none of them reached a human.

It is loud because `for` is `0s`. One 5-minute average crossing the threshold
fires immediately, so the rule reports every transient blip. Per instance:

  next.learn.mit.edu      287 firings, threshold 1000 ms, 7.94% of the window
                          above it, mean crossing ~5.6 min
  api.learn.mit.edu       39 firings,  threshold 3000 ms, 0.60% above,
                          mean crossing ~3.1 min
  learn.mit.edu           7 firings,   threshold 500 ms,  0.17% above,
                          mean crossing ~5.0 min

A 10-minute `for` is placed above every one of those mean crossing durations.
Re-measured against the same 14 days it takes api.learn.mit.edu and
learn.mit.edu to zero firings outright, and cuts next.learn.mit.edu's duty
cycle from 7.94% to 1.46% -- about 5 hours, which is sustained origin slowness
rather than blips, and the part actually worth looking at.

Why a new rule rather than adopting the plugin's
------------------------------------------------
The plugin rule stays exactly where it is, untouched and still in `oblivion`.
Adopting it was considered and rejected: its threshold does not live in the
rule at all but in `sm_alerts_threshold_http_request_duration_too_high_avg`, a
metric the plugin publishes from per-check config in the Synthetic Monitoring
UI. Pulumi would own `for` and the labels while the number that decides when
the rule fires stayed outside IaC -- the same "Pulumi owns an empty shell"
shape this project has now hit three separate times (the Sentry issue alerts,
the CI/QA Rootly route rules' `enabled` flag, the drifted `Low Urgency`
escalation path). Pinning the thresholds in `_Check.latency_ms` instead puts
them in code review and in `git log`, at the cost of not tracking edits made
in the SM UI -- which is the intended direction, not a regression.

That leaves the plugin's rule duplicating the signal, which is harmless: it
delivers nowhere by construction and remains the plugin's own view of its own
checks. Deleting it would be a change to plugin-owned state for no gain.

Routed to Slack, not Rootly, and not because they are unproven
--------------------------------------------------------------
These carry `channel=devops-warnings` for the same reason
metric_rules/apisix_edge.py does: in alertmanager.py's route tree `warning`
and `critical` both terminate at the `rootly` contact point, so a bare
`severity` would page. docs/plans/grafana-alerting-remediation-spec.md W5a
specifies `severity: warning` and notes it is non-paging "once W2b lands" --
W2b has not landed (alertmanager.py's two severity routes are still identical),
so applying that literally today would deliver straight to the on-call.
`channel` sits above both severity routes and terminates before either.

Promote by dropping `channel` from `latency_labels`, once W2b makes `warning`
mean something. `instance` is already in NotificationPolicy.group_bies, so
each probed URL keeps its own notification thread either way.

no_data_state
-------------
"OK", per the convention in base.py. It is load-bearing here rather than
incidental: the threshold is baked into the PromQL, so a healthy check returns
*no series at all*. "NoData" -- what these rules carried before -- would treat
the healthy state as missing data on every single evaluation.

The cost is that a probe which stops reporting entirely no longer alerts here.
The plugin's own ProbeFailedExecutionsTooHigh covers that case.

Severity (fast rules only)
--------------------------
`warning` for the Next.js origin check, `critical` for the two user-facing ones.
The Next.js check deliberately bypasses Fastly (that is what "Bypass Fastly"
means) and hits the origin directly, so it fires on origin degradation the CDN
is still absorbing -- on 2026-08-13 it failed repeatedly while the Fastly-fronted
learn.mit.edu probe held flat at 0.02s. Worth knowing about; not worth waking
someone for. learn.mit.edu and the API health endpoint have no such buffer.

Both routes land on the same Rootly contact point via alertmanager.py's policy
tree. That replaces a `notification_settings.receiver="Rootly"` override on all
three rules, which pointed at a second, UI-created contact point (uid
eel3rjpiwahoge) distinct from Pulumi's `rootly` (uid bfsoqo63lsyrka) and
bypassed the policy tree entirely. `instance` is already in
NotificationPolicy.group_bies, so each probed URL keeps its own notification
thread.
"""

from collections.abc import Callable
from dataclasses import dataclass

from pulumi import ResourceOptions
from pulumiverse_grafana import alerting

from ol_infrastructure.lib.ol_types import Component
from ol_infrastructure.lib.pulumi_helper import parse_stack

# The Synthetic Monitoring plugin's folder. Referenced, never created -- see the
# module docstring.
_SM_FOLDER_UID = "grafana-synthetic-monitoring-app"

# Window and failure-ratio threshold per rule. Both thresholds sit between
# quantisation steps rather than on one -- see the module docstring.
_FAST_WINDOW, _FAST_RATIO, _FAST_FOR = "5m", "0.3", "5m"
_SLOW_WINDOW, _SLOW_RATIO, _SLOW_FOR = "1h", "0.04", "30m"

# Latency rules. The window matches the plugin rule these reclaim; `for_` is the
# whole point of them -- see "Reclaiming the latency signal" in the docstring.
_LATENCY_WINDOW, _LATENCY_FOR = "5m", "10m"

# Route the latency rules to the #devops-warnings Slack channel rather than
# Rootly, exactly as metric_rules/apisix_edge.py does and for the same reason:
# in alertmanager.py's route tree `warning` and `critical` both terminate at the
# `rootly` contact point, so a bare `severity` would page. The `channel` branch
# sits above both severity routes and terminates before either is reached.
_SLACK_CHANNEL = "devops-warnings"


@dataclass(frozen=True)
class _Check:
    """A single synthetic check and the two alert rules that watch it."""

    resource_name: str
    group_name: str
    # UID of the pre-existing hand-made rule, pinned onto the fast rule so the
    # import keeps its identity. The slow rule is new and gets a fresh UID.
    rule_uid: str
    rule_name: str
    slow_rule_name: str
    job: str
    instance: str
    # Typed against the K8s label enum, not `str`: this value and
    # `ol.mit.edu/component` are the two producers of Rootly's `ol_component`
    # routing key, and they agreed on `api`/`nextjs`/`webapp` by coincidence
    # until this annotation made it a guarantee.
    component: Component
    severity: str
    # Average request duration, in ms, above which the latency rule fires.
    # Captured from the plugin's own per-check thresholds
    # (`sm_alerts_threshold_http_request_duration_too_high_avg{period="5m"}`)
    # as they stood on 2026-08-24, then pinned here rather than read from that
    # metric -- see "Reclaiming the latency signal" in the module docstring.
    latency_ms: int
    # Subject of the alert summaries, e.g. "MIT Learn Next.js origin".
    what: str
    # Triage guidance appended to both descriptions.
    context: str


_CHECKS = [
    _Check(
        resource_name="mit-learn-nextjs-check-failed",
        group_name="MIT Learn NextJS",
        rule_uid="afjaps6wtn0n4a",
        rule_name="Learn NextJS Homepage (Bypass Fastly) - Check Failed",
        slow_rule_name=(
            "Learn NextJS Homepage (Bypass Fastly) - Elevated Probe Failure Rate"
        ),
        job="Learn NextJS Homepage (Bypass Fastly)",
        instance="https://next.learn.mit.edu/",
        component=Component.nextjs,
        severity="warning",
        latency_ms=1000,
        what="MIT Learn Next.js origin",
        context=(
            "This check bypasses Fastly and hits the Next.js origin directly, so it "
            "reports origin health rather than what CDN-fronted users see -- compare "
            "the Fastly-fronted learn.mit.edu check before treating it as "
            "user-facing. Failures here are usually 15s timeouts (the check's own "
            "ceiling) rather than HTTP errors; when they are, compare container "
            "working set per ReplicaSet across the most recent rollout of the "
            "mit-learn-nextjs deployment in the mitlearn namespace, because this has "
            "twice been an application memory regression shipped by a deploy rather "
            "than an infrastructure fault."
        ),
    ),
    _Check(
        resource_name="mit-learn-api-check-failed",
        group_name="MIT Learn API",
        rule_uid="ffjan53rjjugwb",
        rule_name="Learn API Health Endpoint - Check Failed",
        slow_rule_name="Learn API Health Endpoint - Elevated Probe Failure Rate",
        job="Learn API Health Endpoint",
        instance="https://api.learn.mit.edu/learn/health",
        component=Component.api,
        severity="critical",
        latency_ms=3000,
        what="MIT Learn API health endpoint",
        context=(
            "This is the API's own health endpoint, so sustained failure means the "
            "API is not serving."
        ),
    ),
    _Check(
        resource_name="mit-learn-website-check-failed",
        group_name="MIT Learn Website",
        rule_uid="ffjaotwo4dblsd",
        rule_name="Learn Homepage - Check Failed",
        slow_rule_name="Learn Homepage - Elevated Probe Failure Rate",
        job="Learn Homepage",
        instance="https://learn.mit.edu/",
        component=Component.webapp,
        severity="critical",
        latency_ms=500,
        what="MIT Learn homepage",
        context=(
            "This is the public homepage through Fastly, so failures here are "
            "user-facing."
        ),
    ),
]


def _failure_ratio_expr(job: str, instance: str, window: str, threshold: str) -> str:
    """Build the probe failure-ratio expression for one check and window.

    Returns a series only when the failure ratio exceeds the threshold, and the
    value it returns rises as the check gets worse, so the downstream `> 0`
    threshold stage reads it correctly. See "Why the expression is inverted" in
    the module docstring -- the naive `avg_over_time(...) < x` form goes silent
    during a total outage.
    """
    return (
        f"1 - avg_over_time("
        f'probe_success{{job="{job}", instance="{instance}"}}[{window}]'
        f") > {threshold}"
    )


def _latency_expr(job: str, instance: str, window: str, threshold_ms: int) -> str:
    """Build the average-request-duration expression for one check.

    Same ratio-of-rates the plugin's own HTTPRequestDurationTooHighAvg uses,
    minus its `sm_check_info` join (that exists to decorate the alert with
    check metadata; the labels this rule needs are already on the series) and
    minus its threshold metric, which is pinned in `_Check.latency_ms` instead.

    The threshold is on the right, so the value carried through is the duration
    in ms -- positive whenever the rule should fire and rising as the endpoint
    gets slower, which is what base.py's `> 0` threshold stage needs.

    `sum by (instance, job)` aggregates the probe locations together, matching
    the plugin rule, so one slow region does not fire on its own. Note this
    drops the `probe` label, unlike the failure-ratio rules above, which read
    the raw series and keep it -- so these annotations must not reference
    `{{ $labels.probe }}`.
    """
    selector = f'{{job="{job}", instance="{instance}"}}'
    return (
        f"("
        f"sum by (instance, job) (rate(probe_all_duration_seconds_sum"
        f"{selector}[{window}]))"
        f" / "
        f"sum by (instance, job) (rate(probe_all_duration_seconds_count"
        f"{selector}[{window}]))"
        f") * 1000 > {threshold_ms}"
    )


def _rules(
    check: _Check, rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]]
) -> list[alerting.RuleGroupRuleArgs]:
    """Build the fast (cliff), slow (creep) and latency rules for one check."""
    # `ol_component`, not a bare `component`: this key is what Rootly's Grafana
    # Production Service Route matches on to reach the per-component services,
    # and `component` is generic enough that a vendor integration or a future
    # rule elsewhere could set it and be routed to a MIT Learn service by
    # accident. Not in alertmanager.py's `group_bies`, so the rename does not
    # regroup any notification.
    base_labels = {"ol_component": check.component, "service": "mitlearn"}
    # The slow rules deliberately carry no `severity` -- see "The slow rules
    # ship unrouted" in the module docstring.
    fast_labels = base_labels | {"severity": check.severity}
    # The latency rules DO carry `severity`, but pair it with `channel` so they
    # land in Slack rather than Rootly -- `channel` terminates above both
    # severity routes. `warning` regardless of the check's own severity: it
    # only picks the Slack formatting here, and slow is not the same as down.
    latency_labels = base_labels | {
        "severity": "warning",
        "channel": _SLACK_CHANNEL,
    }
    return [
        alerting.RuleGroupRuleArgs(
            # Pinned so the imported rule keeps its identity: alert state
            # history, existing silences, and the Grafana rule URL already
            # embedded in every past Rootly alert all key off this UID.
            uid=check.rule_uid,
            name=check.rule_name,
            condition="C",
            for_=_FAST_FOR,
            no_data_state="OK",
            exec_err_state="Error",
            labels=fast_labels,
            annotations={
                "summary": f"{check.what} is failing synthetic probes",
                "description": (
                    f"More than one of the last 5 synthetic probes against "
                    f"{{{{ $labels.instance }}}} from {{{{ $labels.probe }}}} "
                    f"failed, sustained for {_FAST_FOR}. {check.context}"
                ),
            },
            datas=rd(
                _failure_ratio_expr(
                    check.job, check.instance, _FAST_WINDOW, _FAST_RATIO
                )
            ),
        ),
        alerting.RuleGroupRuleArgs(
            name=check.slow_rule_name,
            condition="C",
            for_=_SLOW_FOR,
            no_data_state="OK",
            exec_err_state="Error",
            labels=base_labels,
            annotations={
                "summary": (
                    f"{check.what} has an elevated synthetic probe failure rate"
                ),
                "description": (
                    f"Over 4% of the last hour's synthetic probes against "
                    f"{{{{ $labels.instance }}}} from {{{{ $labels.probe }}}} "
                    f"failed, sustained for {_SLOW_FOR}. This is the window that "
                    f"catches a slow degradation, where no single 5-minute window "
                    f"looks bad enough to trip the paired 'Check Failed' rule. "
                    f"{check.context}"
                ),
            },
            datas=rd(
                _failure_ratio_expr(
                    check.job, check.instance, _SLOW_WINDOW, _SLOW_RATIO
                )
            ),
        ),
        alerting.RuleGroupRuleArgs(
            name=f"{check.job} - Elevated Request Latency",
            condition="C",
            for_=_LATENCY_FOR,
            no_data_state="OK",
            # "OK", not "KeepLast": this is a Slack-routed warning-tier rule,
            # so going quiet through a datasource blip is the cheaper mistake.
            # The paired "Check Failed" rules keep their "Error" state.
            exec_err_state="OK",
            labels=latency_labels,
            annotations={
                "summary": f"{check.what} is responding slowly",
                "description": (
                    f"Average request duration against "
                    f"{{{{ $labels.instance }}}}, averaged across all probe "
                    f"locations, has stayed above {check.latency_ms} ms for "
                    f"{_LATENCY_FOR}. "
                    f"This measures how slowly the endpoint answers, not "
                    f"whether it answers at all -- the paired 'Check Failed' "
                    f"and 'Elevated Probe Failure Rate' rules cover "
                    f"availability. {check.context}"
                ),
            },
            datas=rd(
                _latency_expr(
                    check.job, check.instance, _LATENCY_WINDOW, check.latency_ms
                )
            ),
        ),
    ]


def create(
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create the MIT Learn synthetic monitoring alert rule groups.

    Takes no `folder_uid`: these rules live in the Synthetic Monitoring plugin's
    folder, not the Pulumi-created "Infrastructure Alerts" one. Production only
    -- that folder's UID differs per Grafana stack and the checks these watch
    exist nowhere else. See the module docstring.
    """
    if parse_stack().env_suffix != "production":
        return

    for check in _CHECKS:
        alerting.RuleGroup(
            check.resource_name,
            name=check.group_name,
            folder_uid=_SM_FOLDER_UID,
            interval_seconds=300,
            rules=_rules(check, rd),
            opts=resource_opts,
        )
