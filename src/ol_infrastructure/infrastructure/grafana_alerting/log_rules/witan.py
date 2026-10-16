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

WitanGraphBlockedAtStartup
----------------------------
The blocked-graph detector, at the one moment it is decidable: startup. On
omnigraph 0.13 a graph that fails to open is blocked rather than fatal, and the
server logs one WARN line per graph it could not open:

    graph blocked during startup graph_id=<id> error=open graph '<id>' at ...

while /readyz stays 200 (``"status":"degraded"``, ``blocked_graph_count`` > 0)
as long as at least one graph is ready. The rule counts those lines over a 6h
window.

It replaced ``WitanGraphQuarantined``, which compared the ``graph_count`` on the
``serving omnigraph`` boot line against its weekly high. That worked on 0.11,
where a graph that failed to open was absent from the count. From 0.12 on the
line is logged BEFORE any graph loads and N is the number of graphs cluster.yaml
DECLARES, so the comparison could not fire. It was deleted once every
environment ran 0.13.

★ WHAT THIS MEASURES: "ANY DEGRADED BOOT IN THE LAST 6 HOURS", not the newest
boot. LogQL has no way to pick the chronologically latest sample across series
(each boot is a different pod), so the rule is defined as the event it can
detect. That is also the more useful framing: a boot that came up with a graph
missing is worth investigating even if a later restart recovered, because the
condition existed and can recur, and the recovering restart is exactly what
would otherwise erase the evidence. It keeps firing for up to 6 hours after a
recovering restart, so check the running pod before assuming an active outage.

The window is empty except in the hours after a boot. That is deliberate: this
is a transition detector, and outside that window there is no new evidence to
evaluate.

It also resolves 6 hours after the boot while the graph is still blocked,
because 0.13 does not retry a blocked graph until the next restart and so logs
nothing more. At storage format 14 the nightly maintenance window restarts the
server, so a graph that stays blocked fires for about 6 hours after each
window and then goes quiet. Ongoing user-visible impact is covered by
WitanToolCallErrorRate instead, which is why neither rule is redundant with the
other: this one is not traffic-dependent but only speaks at boot, and that one
speaks continuously but only while there is traffic.

A LINE FILTER, unlike every other omnigraph rule here, and deliberately. The
escapes sit around field names and values, not inside the message text, so the
message is contiguous on the wire. Filtering on it also avoids running
``logfmt`` over the ``error`` field, which is unquoted and contains spaces,
commas and colons.

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

The CI indexer (CronJob ``witan-ci-indexer``, namespace ``witan``) runs every 4
hours in EVERY environment and prints one line per repo:

    index .: scanned=2017 indexed=0 skipped=1989 symbols=1013 edges=2180 bindings=221 errors=0

★ RUNS IN ALL THREE CLUSTERS, NOT JUST PRODUCTION. An earlier revision of this
rule assumed ``operations-production`` was the only cluster running the
indexer -- ``omnigraph:managed_repos`` is non-empty in every environment's
config, and ``create_ci_indexer`` is gated only on that list being non-empty,
with no environment check. Confirmed directly against the live CronJobs, not
inferred from config: ``kubectl -n witan get cronjob witan-ci-indexer`` shows
it un-suspended and on-schedule (``LAST SCHEDULE`` a few hours old) in
``operations-ci``, ``operations-qa`` AND ``operations-production`` alike. The
expression below is therefore cluster-filtered and the rule is a
non-prod/prod pair, the same shape as ``WitanCedarDenials*`` and
``WitanGraphBlockedAtStartup*`` above -- omitting the split here was the
mistake, not the norm. Caught in review by copilot-pull-request-reviewer.

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
falls in the cycle it may span TWO cycles rather than one; that delays firing
(both cycles' lines must sum to zero) rather than suppressing it outright for
a SUSTAINED failure -- the previous good cycle's lines keep ageing out of the
window as the bad condition persists, so a fleet-wide outage lasting more than
one cycle (like the 15h wedge this rule exists for) is always eventually
caught. "Within about an hour" only holds when the previous good cycle
finished quickly; the indexer's own ceiling is 3h
(``INDEX_ACTIVE_DEADLINE_SECONDS`` in ``ci_indexer.py``), so in the worst case
(a slow ~3h good cycle immediately followed by a bad one) residual positive
data can persist for up to roughly 4h into the bad cycle, not ~1h.

★ KNOWN LIMIT, NOT COVERED BY THE BACKTEST: an ISOLATED single bad cycle,
immediately preceded AND followed by long-duration (~3h) good cycles, could in
the worst case never produce a window with zero total, because a new good
cycle starts writing positive lines again before the old one's residue fully
clears. This rule's actual design target is a SUSTAINED failure -- the
incident that motivated it lasted ~15 hours, many cycles, nowhere near this
edge -- and the 6.9-day backtest below never exercised this combination
because no real all-zero cycle occurred in that window to test it against.
Catching a genuinely isolated one-cycle blip with certainty needs a per-cycle
run identifier and a completion signal the indexer does not currently emit;
out of scope here. Caught in review by copilot-pull-request-reviewer.

NOT covered here: the indexer not running at all. No lines means no series,
which is NoData, which is OK. That case belongs to
``WitanScheduledJobNeverSucceeded`` and eks_general.py's CronJob staleness
rules in principle -- except ``witan-ci-indexer`` is explicitly EXCLUDED from
both today (known intermittent-failure break,
``tk-witan-ci-indexer-cronjob-fails-on-nearly-every-r-4c1462``), so a total
stop of the indexer is presently an uncovered gap, not a handled case. Caught
in review by copilot-pull-request-reviewer.

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
  the two cycles preceding 2026-09-01T20:55Z, matching the SUM of both cycles'
  hand counts -- 932 at 16:00Z plus 850 at 20:00Z -- not the 850 from the
  20:00Z cycle alone. Caught in review by copilot-pull-request-reviewer.

``WitanCedarDenials`` was evaluated against the production Loki tenant on
2026-08-24 and shown to be able to FIRE rather than only to be quiet: it
returns nothing as committed, and the identical expression with
``allowed="true"`` returned 5 change + 77 read over the same 10-minute window.

``WitanGraphBlockedAtStartup`` was written 2026-10-13, before any environment
ran 0.13. What was checked then:

  The line itself, from the released 0.13.0 server booted locally on a
  two-graph ``file://`` root with one graph's storage moved aside. On the wire
  it is ``... graph blocked during startup <ESC>[3mgraph_id<ESC>[0m<ESC>[2m=
  <ESC>[0mbeta ...``: the message text carries no escapes. The same boot
  logged ``graph_count=2`` and answered /readyz 200 with
  ``"ready_graph_count":1,"blocked_graph_count":1``.
  That a line filter on message text matches this stream: the identical
  expression with ``"serving omnigraph"`` returns 1 on the production tenant
  for the hour after the 2026-10-12T18:58Z boot.
  The expression as committed parses and returns nothing on the production
  tenant (132,480 lines scanned).

It has since fired in QA. A ``graph blocked during startup`` line landed in the
operations-qa stream in the hour to 2026-10-15T18:00Z, and the QA Grafana state
history for ``WitanGraphBlockedAtStartupWarning`` shows Pending at 17:59:50Z,
Alerting at 18:09:50Z (the 10m pending period) and Normal (NoData) at
23:59:50Z, when the line aged out of the 6h window.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

_NON_PROD_CLUSTERS = ".*-(ci|qa)"
_PROD_CLUSTERS = ".*-(production)"

# How far back a boot still counts as "recent". Every blocked-graph line inside
# this window counts -- see the module docstring on why this is "any degraded
# boot in the last 6 hours" rather than "the newest boot". Outside the window
# there are no such lines and the rule evaluates to nothing.
_GRAPH_RECENT_BOOT_WINDOW = "6h"

# The message omnigraph 0.13 logs once per graph it could not open at startup.
_GRAPH_BLOCKED_MESSAGE = "graph blocked during startup"

_OMNIGRAPH_STREAM = 'namespace="omnigraph", container="omnigraph-server"'

# The CI indexer's stream. It runs in every cluster (ci/qa/production alike --
# see the module docstring), so this needs the same `cluster=~` filter the
# omnigraph rules above use.
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

_GRAPH_BLOCKED_SUMMARY = (
    "omnigraph in {{ $labels.cluster }} booted in the last 6h with a graph it"
    " could not open"
)

_GRAPH_BLOCKED_DESCRIPTION = (
    "At least one omnigraph-server boot in cluster {{ $labels.cluster }} in the"
    " last 6 hours logged `graph blocked during startup`. On omnigraph 0.13 a"
    " graph that cannot be opened is blocked rather than fatal: the pod goes"
    " Ready, /readyz returns 200 with status `degraded`, and only requests"
    " against that graph fail (503 graph_unavailable). If the blocked graph is"
    " `council`, every agent on the service is broken."
    " Find which graph and why with"
    ' `{namespace="omnigraph", container="omnigraph-server"} |='
    ' "graph blocked during startup"`: each line carries graph_id and the open'
    " error."
    " This is an 'any degraded boot in 6 hours' alert, not a current-state one."
    " Check the RUNNING pod before treating it as an active outage:"
    " `kubectl -n omnigraph port-forward deploy/omnigraph-server 8080` and then"
    " `curl -s localhost:8080/readyz` reports blocked_graph_count (the image"
    " has no curl to exec). A restart that already recovered keeps this firing"
    " until the degraded boot ages out of the window."
    " The reverse also holds: a graph that stays blocked is not retried until"
    " the next restart, and this alert RESOLVES 6 hours after the boot anyway."
    " Resolved does not mean recovered. Read blocked_graph_count."
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
    " it is NoData, not a fire. That case is NOT currently covered:"
    " witan-ci-indexer is explicitly excluded from both"
    " WitanScheduledJobNeverSucceeded and eks_general.py's CronJob staleness"
    " rules (known intermittent-failure break,"
    " tk-witan-ci-indexer-cronjob-fails-on-nearly-every-r-4c1462). A total"
    " stop of the indexer is a known gap, not a handled case."
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


def _graph_blocked_expr(cluster_filter: str) -> str:
    """Blocked-graph lines logged at startup over the recent-boot window.

    A line filter on the message, not a parsed label. See the module docstring
    for why that is safe on this stream for message text and nothing else.

    Grouped by ``cluster`` alone, like ``_cedar_denial_expr``: ``graph_id`` is
    not in the notification policy's ``group_bies``. The value is the line
    count, one per blocked graph per boot, so it is positive whenever a row
    exists.
    """
    return (
        "sum by (cluster) (\n"
        "  count_over_time(\n"
        f'    {{{_OMNIGRAPH_STREAM}, cluster=~"{cluster_filter}"}}\n'
        f'    |= "{_GRAPH_BLOCKED_MESSAGE}" [{_GRAPH_RECENT_BOOT_WINDOW}]\n'
        "  )\n"
        ") > 0"
    )


def _no_bindings_expr(cluster_filter: str) -> str:
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

    ``cluster_filter``: the indexer runs in every environment (see the module
    docstring), so this needs the same non-prod/prod split ``_cedar_denial_expr``
    and ``_graph_blocked_expr`` already use -- an earlier revision omitted it, which would have hard-labeled a CI/QA-only outage as production.
    """
    matched = f'{{{_CI_INDEXER_STREAM}, cluster=~"{cluster_filter}"}} |= "bindings=" | regexp "bindings=(?P<bindings_written>[0-9]+)"'
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
            # --- Blocked graph, caught at boot ---
            # In the slot the 0.11 WitanGraphQuarantined* pair held, so the
            # rules after it keep their list positions.
            alerting.RuleGroupRuleArgs(
                name="WitanGraphBlockedAtStartupWarning",
                condition="C",
                for_="10m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", "environment": "non-production"},
                annotations={
                    "summary": _GRAPH_BLOCKED_SUMMARY,
                    "description": _GRAPH_BLOCKED_DESCRIPTION,
                },
                datas=rd(_graph_blocked_expr(_NON_PROD_CLUSTERS)),
            ),
            alerting.RuleGroupRuleArgs(
                name="WitanGraphBlockedAtStartupCritical",
                condition="C",
                for_="10m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical", "environment": "production"},
                annotations={
                    "summary": _GRAPH_BLOCKED_SUMMARY,
                    "description": _GRAPH_BLOCKED_DESCRIPTION,
                },
                datas=rd(_graph_blocked_expr(_PROD_CLUSTERS)),
            ),
            # --- Cross-repo bridge wrote nothing for a whole cycle ---
            # A prod/non-prod pair, like WitanCedarDenials* and
            # WitanGraphBlockedAtStartup* above: the CI indexer runs in every
            # environment (see the module docstring), so a rule with no
            # cluster filter would have hard-labeled a CI/QA-only outage as
            # production.
            #
            # Warning, not critical, in both environments. A quiet bridge
            # degrades cross-repo answers -- `code_interface_*` stops
            # resolving across repos -- but leaves every per-repo graph and
            # all of witan's memory and task surface working. It wants
            # somebody to look during the day, which is what the previous
            # 15-hour production outage actually needed and did not get.
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
                labels={"severity": "warning", "environment": "non-production"},
                annotations={
                    "summary": _NO_BINDINGS_SUMMARY,
                    "description": _NO_BINDINGS_DESCRIPTION,
                },
                datas=rd(_no_bindings_expr(_NON_PROD_CLUSTERS)),
            ),
            alerting.RuleGroupRuleArgs(
                name="WitanCodeBridgeNoBindingsProdWarning",
                condition="C",
                for_="0m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", "environment": "production"},
                annotations={
                    "summary": _NO_BINDINGS_SUMMARY,
                    "description": _NO_BINDINGS_DESCRIPTION,
                },
                datas=rd(_no_bindings_expr(_PROD_CLUSTERS)),
            ),
        ],
        opts=resource_opts,
    )
