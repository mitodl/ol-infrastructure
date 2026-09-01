"""Log-derived alert rules for the shared witan MCP service.

Written 2026-08-24, alongside metric_rules/witan.py. Read that module's
docstring first: it explains why witan's worst failure mode is invisible to
both health endpoints, and this module is the half of the answer that does not
depend on there being live traffic.

★ omnigraph-server LOGS WITH ANSI ESCAPES, AND A LINE FILTER CANNOT SEE THROUGH
  THEM
--------------------------------------------------------------------------------
It is a Rust ``tracing`` binary that writes styled output even when stderr is
not a tty, and the escapes sit BETWEEN a field name, its ``=`` and its value.
A line that reads

    policy decision actor_id="act-..." action=read allowed=true

on screen is, on the wire:

    ... allowed<ESC>[0m<ESC>[2m=<ESC>[0mtrue ...

so ``|= "allowed=true"`` matches nothing. It does not error -- it returns a
confident zero, which is the worst possible failure for an alert rule, since a
filter that finds nothing is indistinguishable from a condition that is fine.
Measured on 2026-08-24 over 7 days of the production stream: that filter found
0 lines while ``| decolorize | logfmt`` over the same stream and window found
21,108.

Every rule here therefore strips the escapes with ``decolorize`` FIRST and then
matches on PARSED LABELS, never on raw line content. Note that a line filter
placed after ``decolorize`` is not sufficient either -- Loki pushes line
filters down the pipeline, so ``| decolorize |= "allowed=true"`` also returns
zero. Only the parsed-label form works.

witan-server is unaffected -- it emits real JSON via structlog -- but nothing
here reads it; the metric side covers witan's own behaviour.

WitanCedarDenials
-------------------
The Cedar denial rate the shared-service design asked for. In the 7 days to
2026-08-24 production recorded 21,108 policy decisions and every one was
``allowed=true``, so this is a genuinely quiet signal and ``> 0`` is a real
threshold rather than one picked out of the air.

A denial is not automatically an incident: a user who legitimately lacks a
grant looks identical to a policy deploy that removed one. That is why it is a
warning even in production, and why the description says to read
``matched_rule_id`` rather than treating it as an outage.

WitanGraphQuarantined
-----------------------
The quarantine detector, at the one moment quarantine is decidable: startup.
omnigraph-server logs how many graphs it actually opened --

    serving omnigraph bind=0.0.0.0:8080 mode="cluster" graph_count=16 ...

-- and a graph that failed to open is simply absent from that count while the
process goes on serving. Comparing against the highest count seen over the past
week makes this self-calibrating: adding a managed repo raises the maximum on
the same boot that raises the current value, so growth never fires, and no
declared-graph count has to be duplicated here where it would drift.

★ WHAT THIS ACTUALLY MEASURES: "ANY REDUCED BOOT IN THE LAST 6 HOURS".
Not "the newest boot", which an earlier revision of this docstring claimed and
the expression never did. ``min_over_time`` takes the LOWEST count across every
boot in the window, and LogQL has no way to pick the chronologically latest
sample across series -- each boot is a different pod, so ``last_over_time`` is
per-pod and taking a min across those pods lands back on the same value.
Rather than approximate it, the rule is defined as the event it can actually
detect, and the operational guidance below matches. Caught in review by
copilot-pull-request-reviewer.

That framing is also the more useful one. A boot that came up with a graph
missing is worth investigating even if a later restart recovered -- the
condition existed and can recur, and the recovering restart is exactly what
would otherwise erase the evidence.

The 6h side is empty except in the hours after a boot. That is deliberate --
this is a transition detector, and outside that window there is no new evidence
to evaluate.

★ THREE KNOWN LIMITS, ALL DELIBERATE.
1. A graph deliberately REMOVED from cluster.yaml fires this for a week until
   the baseline re-learns. Production has done this once: the count stepped
   17 -> 16 between 2026-08-09 and 2026-08-11, alongside the storage-format-6
   cutover (#5372). Silence it for the week, or confirm the removal was
   intended -- a graph that disappears across a storage migration is worth
   confirming rather than assuming.
2. It keeps firing for up to 6 hours after a recovering restart, because the
   reduced boot stays in the window. That is the "any reduced boot" semantics
   working as defined, not a bug to tune out. Check the boot lines before
   assuming an active outage.
3. A deploy that ADDS a graph can false-fire for up to 6 hours if a boot from
   before the addition is still in the window: the older boot sets the minimum
   while the newer one raises the week's maximum. It needs two boots within 6h
   straddling the change, and clears itself.
Conversely, a quarantine that survives a week of restarts stops firing once the
baseline learns the lower count. Ongoing user-visible impact is covered by
WitanToolCallErrorRate instead, which is why neither rule is redundant with the
other: this one is not traffic-dependent but only speaks at boot, and that one
speaks continuously but only while there is traffic.

WitanCodeBridgeNoBindings
---------------------------
The cross-repo bridge going quiet, keyed on the OUTCOME rather than on an
exception. On 2026-08-25 the production bridge graph wedged on a stranded
`Armed` write intent and stayed wedged for roughly 15 hours before a human
noticed. Its signature is the reason this rule exists: every repo reported
``errors=0`` while writing zero bindings, so nothing raised, nothing logged at
ERROR, and Sentry had nothing to catch. agent-kit#288 later made a bridge write
that RAISES loud, and omnigraph #561 fixed that particular wedge -- but both
are keyed to a known mechanism, and a detector keyed to the result is
independent of whichever mechanism comes next.

The CI indexer (CronJob ``witan-ci-indexer``, namespace ``witan``, cluster
``operations-production``) runs every 4 hours and prints one line per repo:

    index .: scanned=2017 indexed=0 skipped=1989 symbols=1013 edges=2180 bindings=221 errors=0

★ FLEET-WIDE, NEVER PER-REPO. A per-repo ``bindings=0`` rule would fire
constantly on healthy runs. Measured over the two consecutive 2026-09-01 cycles
(16:00Z and 20:00Z, 14 repos each): 8 of 14 reported zero in the 20:00Z cycle,
and 7 of the 14 reported zero in BOTH. There is more than one innocent reason
for it -- one repo declared nothing to bind across both cycles (``scanned=1582``
wrote ``bindings=0`` at ``indexed=30`` and again at ``indexed=23``), while
another went ``bindings=119`` -> ``0`` as its ``indexed`` dropped 1 -> 0. Either
way the per-repo signal is noise, and a rule built on it would be silenced
within a day. The defensible trigger is the whole cycle summing to zero.

★ ZERO, NOT A FLOOR. The fleet total moves cycle to cycle on healthy runs --
932 at 16:00Z and 850 at 20:00Z on 2026-09-01 -- and an earlier measurement
recorded in the task has one repo drifting 248 -> 123 -> 110 -> 104 over three
cycles before settling. Six healthy cycles spanned 657-821. Any absolute floor
sized against that would false-positive; only zero is defensible without more
baseline than we have.

★ COULD A GENUINELY QUIET CYCLE BE FLEET-ZERO? It is the obvious false positive
for this rule, and it is answered empirically rather than by argument: across
the 6.9 days from 2026-08-26T00:00Z -- which includes the 2026-08-29/30 weekend
-- no 5h window was ever fleet-zero. The expression returns a real 0 at every
hourly step over that whole span.

★ WHY THE EXPRESSION IS A PRODUCT AND NOT ``== 0``. base.py's stage C fires on
``last(A) > 0``, so the obvious ``sum(...) == 0`` is exactly wrong: it returns
a row carrying the value 0 and therefore never fires. The rule multiplies the
line count by ``(total == bool 0)``, which is the line count in the bad case
and a real 0 in the good one. A real 0 also matters for a second reason -- an
alert that evaluates to a visible zero can be told apart from one that is
silently broken, which two of this project's earlier measurements could not.

The 5h window is longer than the 4h cycle so a single cycle is always fully
covered, with margin for a late or slow run. Depending on where evaluation
falls in the cycle it may span TWO cycles rather than one; that only ever
delays firing (both cycles must be zero), never suppresses it -- the previous
good cycle ages out within about an hour of the bad one.

NOT covered here: the indexer not running at all. No lines means no series,
which is NoData, which is OK. That case belongs to
``WitanScheduledJobNeverSucceeded`` and eks_general.py's CronJob staleness
rules, which watch the schedule rather than its output.

Verification
--------------
``WitanCodeBridgeNoBindings`` was backtested against the production Loki tenant
on 2026-09-01, in both directions rather than only for quiet:

  During the 2026-08-25 wedge the expression returns 24-71 continuously from
  09:30Z to 20:00Z, and drops back to 0 at 20:30Z when the bridge recovered --
  so the rule would have fired within about an hour of the failing cycle
  instead of the ~15 hours it actually took.
  Over the 6.9 days from 2026-08-26T00:00Z to 2026-09-01T21:00Z it returns a
  real 0 at every hourly step, with no NoData gaps: the instrument is live and
  quiet, not absent.
  The underlying ``sum_over_time`` of the unwrapped counts reads 1782 across
  the two cycles preceding 2026-09-01T20:55Z, matching the 850 counted by hand
  off the 20:00Z cycle's 14 lines.

Both expressions written 2026-08-24 were evaluated against the production Loki
tenant that day, and both were shown to be able to FIRE rather than only to be
quiet:

  WitanCedarDenials     returns nothing as committed; the identical expression
                        with ``allowed="true"`` returned 5 change + 77 read
                        over the same 10-minute window.
  WitanGraphQuarantined returns a real 0 (both sides present, not an empty
                        result) with the 7-day baseline committed here; with
                        the baseline widened to 30 days it returns 1, correctly
                        catching the 17 -> 16 step described above. Note the
                        VALUE, not just the row: base.py's pipeline fires on
                        ``last(A) > 0``, so an expression that returns a row
                        carrying 0 never fires. See metric_rules/witan.py's
                        ``_never_succeeded_expr`` for the revision of this PR
                        that got that wrong.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

_NON_PROD_CLUSTERS = ".*-(ci|qa)"
_PROD_CLUSTERS = ".*-(production)"

# How far back to look for the highest graph count the fleet has served. Long
# enough to span a weekend with no restarts; short enough that a deliberate
# graph removal stops firing within a week rather than a month.
_GRAPH_BASELINE_WINDOW = "7d"

# How far back a boot still counts as "recent". Every boot inside this window
# is considered, and the LOWEST count among them is what gets compared -- see
# the module docstring on why this is "any reduced boot in the last 6 hours"
# rather than "the newest boot". Outside the window there are no boot samples
# at all and the rule evaluates to nothing.
_GRAPH_RECENT_BOOT_WINDOW = "6h"

_OMNIGRAPH_STREAM = 'namespace="omnigraph", container="omnigraph-server"'

# The CI indexer's stream. It runs in exactly one cluster
# (`operations-production`), so unlike the omnigraph rules above there is no
# prod/non-prod split -- the CI and QA Grafana stacks have their own Loki
# tenants, where this selector simply returns nothing.
_CI_INDEXER_STREAM = 'namespace="witan", service_name="witan-ci-indexer"'

# Longer than the indexer's 4h cycle so one cycle is always fully covered. See
# the module docstring on why spanning two cycles is acceptable.
_BINDINGS_WINDOW = "5h"

_CEDAR_SUMMARY = (
    "omnigraph is denying requests under Cedar policy in {{ $labels.cluster }}"
)

_CEDAR_DESCRIPTION = (
    "omnigraph-server in cluster {{ $labels.cluster }} denied at least one"
    " request under Cedar policy in the last 10 minutes. Production's baseline"
    " is zero denials over 7 days, so this is worth reading rather than"
    " tuning: it is either a user without the grant they need, or a policy"
    " deploy that removed one. Query the decisions with"
    ' `{namespace="omnigraph", container="omnigraph-server"} | decolorize |'
    ' logfmt | allowed="false"` and read actor_id and matched_rule_id together'
    " -- the rule id names which policy denied, and its ABSENCE means nothing"
    " matched and the default deny applied, which is a different problem."
    " Note the decolorize: omnigraph writes ANSI escapes between a field name"
    " and its value, so a plain line filter for allowed=false silently matches"
    " nothing."
)

_QUARANTINE_SUMMARY = (
    "omnigraph in {{ $labels.cluster }} booted in the last 6h serving"
    " {{ $value }} fewer graphs than its weekly high"
)

_QUARANTINE_DESCRIPTION = (
    "At least one omnigraph-server boot in cluster {{ $labels.cluster }} in the"
    " last 6 hours came up serving fewer graphs than the most it has served in"
    " the past week. OMNIGRAPH_REQUIRE_ALL_GRAPHS is deliberately unset, so a"
    " graph that cannot be opened is quarantined rather than fatal: the pod goes"
    " Ready, /healthz returns 200, and only requests against the missing graph"
    " fail. If the missing graph is `council`, every agent on the service is"
    " broken and no other signal reports it."
    " ★ THIS IS AN 'ANY REDUCED BOOT IN 6 HOURS' ALERT, NOT A CURRENT-STATE ONE."
    " Check whether the condition is still live before treating it as an active"
    " outage: `kubectl -n omnigraph logs deploy/omnigraph-server | head -50` on"
    " the RUNNING pod, and compare the graphs it names against the declared list"
    " in cluster.yaml. A restart that already recovered will keep this firing"
    " until the reduced boot ages out of the window, which is deliberate -- the"
    " condition existed and can recur."
    " If a graph was removed on purpose, silence this for a week while the"
    " baseline re-learns, and confirm the removal really was intended, because a"
    " graph lost during a storage migration looks exactly the same from here."
)


_NO_BINDINGS_SUMMARY = (
    "the witan code bridge wrote zero cross-repo bindings across a whole"
    " indexer cycle in {{ $labels.cluster }}"
)

_NO_BINDINGS_DESCRIPTION = (
    "Every repo the CI indexer touched in the last 5 hours reported"
    " `bindings=0`, so the shared cross-repo bridge graph took no writes at"
    " all. `code_interface_providers` / `code_interface_consumers` and every"
    " cross-repo answer built on them degrade silently while this holds --"
    " nothing raises and nothing reaches Sentry, which is why this rule reads"
    " the OUTCOME rather than waiting for an exception. That is exactly how the"
    " 2026-08-25 wedge stayed invisible for ~15 hours: `errors=0` on every"
    " repo, zero bindings on every repo."
    " ★ CHECK IT IS THE FLEET, NOT ONE REPO. Half the fleet reports"
    " `bindings=0` on a perfectly healthy cycle -- 8 of 14 repos in the"
    " 2026-09-01 20:00Z run -- either because the repo declares nothing to bind"
    " or because nothing in it changed that cycle. Only the whole cycle summing"
    " to zero means anything. Read the cycle with"
    ' `{namespace="witan", service_name="witan-ci-indexer"} |='
    ' "bindings="` and confirm no repo wrote any.'
    " Then look at the bridge graph itself: a stranded `Armed` write intent on"
    " `code-bridge` was the 2026-08-25 mechanism (omnigraph #561, shipped in"
    " v0.10.0, heals it), and `kubectl -n witan logs job/<newest witan-ci-index"
    " job>` carries the per-repo lines with any error context."
    " If the indexer stopped running entirely this rule says nothing --"
    " it is NoData, not a fire. WitanScheduledJobNeverSucceeded and the"
    " CronJob staleness rules cover that case."
)


def _cedar_denial_expr(cluster_filter: str) -> str:
    """Cedar denials over 10 minutes, per cluster.

    Grouped by ``cluster`` alone rather than also by ``action``: ``action`` is
    not in the notification policy's ``group_bies``, so grouping on it would
    collapse every firing instance into one notification group instead of
    bundling properly. The action is in the log line the description sends the
    reader to.
    """
    return (
        "sum by (cluster) (\n"
        "  count_over_time(\n"
        f'    {{{_OMNIGRAPH_STREAM}, cluster=~"{cluster_filter}"}}\n'
        '    | decolorize | logfmt | __error__="" | allowed="false" [10m]\n'
        "  )\n"
        ") > 0"
    )


def _graph_quarantine_expr(cluster_filter: str) -> str:
    """Drop between the week's highest opened-graph count and the lowest recent one.

    ``min_over_time`` over the recent-boot window, NOT the newest boot -- see
    the module docstring for why LogQL cannot express the latter and why the
    weaker statement is the more useful one anyway.

    ``graph_count != ""`` selects only the one boot line that carries the
    field, so ``unwrap`` never sees a line it cannot parse.

    The subtraction is what propagates to base.py's ``last(A) > 0`` threshold,
    so the value that fires is the size of the drop -- positive by
    construction, and rendered by ``{{ $value }}`` in the summary.
    """
    stream = f'{{{_OMNIGRAPH_STREAM}, cluster=~"{cluster_filter}"}}'
    parsed = '| decolorize | logfmt | graph_count != "" | unwrap graph_count'
    return (
        "(\n"
        "  max by (cluster) (\n"
        f"    max_over_time({stream} {parsed} [{_GRAPH_BASELINE_WINDOW}])\n"
        "  )\n"
        "  -\n"
        "  min by (cluster) (\n"
        f"    min_over_time({stream} {parsed} [{_GRAPH_RECENT_BOOT_WINDOW}])\n"
        "  )\n"
        ") > 0"
    )


def _no_bindings_expr() -> str:
    """Index lines in the window when the fleet wrote zero bindings, else 0.

    A PRODUCT, not a comparison, and the shape is load-bearing. base.py's stage
    C fires on ``last(A) > 0``, so the natural ``sum(...) == 0`` returns a row
    carrying 0 and never fires. Multiplying the line count by the ``bool``
    comparison yields the count when the total is zero and a real 0 when it is
    not -- which fires correctly AND stays visibly alive in the rule's history,
    so a broken rule can be told from a quiet one.

    Both halves parse the same lines the same way rather than sharing a
    sub-expression, because LogQL has no way to bind one. ``regexp`` (not a
    line filter on ``bindings=0``) because the value is what matters and the
    count of lines carrying any value is the other half of the test.

    Grouped ``by (cluster)`` so the alert carries a resource-identifying label:
    the notification policy groups on ``cluster``, and both sides carry the
    identical label set so the binary operation matches cleanly.
    """
    matched = f'{{{_CI_INDEXER_STREAM}}} |= "bindings=" | regexp "bindings=(?P<bindings_written>[0-9]+)"'
    return (
        "sum by (cluster) (\n"
        f"  count_over_time({matched} [{_BINDINGS_WINDOW}])\n"
        ")\n"
        "*\n"
        "(\n"
        "  sum by (cluster) (\n"
        f"    sum_over_time({matched} | unwrap bindings_written [{_BINDINGS_WINDOW}])\n"
        "  ) == bool 0\n"
        ")"
    )


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create the witan log-derived alert rule group."""
    alerting.RuleGroup(
        "loki-witan-service",
        name="witan-service",
        folder_uid=folder_uid,
        interval_seconds=300,
        rules=[
            # --- Cedar denials ---
            # Warning in both environments. A denial is a policy question, not
            # an availability one, and the production baseline is zero either
            # way -- there is nothing here that a critical would buy.
            alerting.RuleGroupRuleArgs(
                name="WitanCedarDenialsWarning",
                condition="C",
                for_="5m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", "environment": "non-production"},
                annotations={
                    "summary": _CEDAR_SUMMARY,
                    "description": _CEDAR_DESCRIPTION,
                },
                datas=rd(_cedar_denial_expr(_NON_PROD_CLUSTERS)),
            ),
            alerting.RuleGroupRuleArgs(
                name="WitanCedarDenialsProdWarning",
                condition="C",
                for_="5m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", "environment": "production"},
                annotations={
                    "summary": _CEDAR_SUMMARY,
                    "description": _CEDAR_DESCRIPTION,
                },
                datas=rd(_cedar_denial_expr(_PROD_CLUSTERS)),
            ),
            # --- Quarantined graph, caught at boot ---
            alerting.RuleGroupRuleArgs(
                name="WitanGraphQuarantinedWarning",
                condition="C",
                for_="10m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", "environment": "non-production"},
                annotations={
                    "summary": _QUARANTINE_SUMMARY,
                    "description": _QUARANTINE_DESCRIPTION,
                },
                datas=rd(_graph_quarantine_expr(_NON_PROD_CLUSTERS)),
            ),
            alerting.RuleGroupRuleArgs(
                name="WitanGraphQuarantinedCritical",
                condition="C",
                for_="10m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical", "environment": "production"},
                annotations={
                    "summary": _QUARANTINE_SUMMARY,
                    "description": _QUARANTINE_DESCRIPTION,
                },
                datas=rd(_graph_quarantine_expr(_PROD_CLUSTERS)),
            ),
            # --- Cross-repo bridge wrote nothing for a whole cycle ---
            # One rule, not a prod/non-prod pair: the CI indexer runs only in
            # operations-production, and the other stacks' Loki tenants return
            # no data for this selector.
            #
            # Warning, not critical. A quiet bridge degrades cross-repo answers
            # -- `code_interface_*` stops resolving across repos -- but leaves
            # every per-repo graph and all of witan's memory and task surface
            # working. It wants somebody to look during the day, which is what
            # the previous 15-hour outage actually needed and did not get.
            #
            # for_="0m": stage A already integrates over 5 hours, so a
            # pending period would only add latency to a condition that is by
            # construction not transient.
            alerting.RuleGroupRuleArgs(
                name="WitanCodeBridgeNoBindingsWarning",
                condition="C",
                for_="0m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", "environment": "production"},
                annotations={
                    "summary": _NO_BINDINGS_SUMMARY,
                    "description": _NO_BINDINGS_DESCRIPTION,
                },
                datas=rd(_no_bindings_expr()),
            ),
        ],
        opts=resource_opts,
    )
