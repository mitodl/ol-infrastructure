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
process goes on serving. Comparing the newest boot against the highest count
seen over the past week makes this self-calibrating: adding a managed repo
raises the maximum on the same boot that raises the current value, so growth
never fires, and no declared-graph count has to be duplicated here where it
would drift.

The 6h side is empty except in the hours after a boot. That is deliberate --
this is a transition detector, and outside that window there is no new evidence
to evaluate.

★ TWO KNOWN LIMITS, BOTH DELIBERATE.
A graph deliberately REMOVED from cluster.yaml fires this for a week until the
baseline re-learns. Production has done this once: the count stepped 17 -> 16
between 2026-08-09 and 2026-08-11, alongside the storage-format-6 cutover
(#5372). Silence it for the week, or confirm the removal was intended -- a
graph that disappears across a storage migration is worth confirming rather
than assuming.
Conversely, a quarantine that survives a week of restarts stops firing once the
baseline learns the lower count. Ongoing user-visible impact is covered by
WitanToolCallErrorRate instead, which is why neither rule is redundant with the
other: this one is not traffic-dependent but only speaks at boot, and that one
speaks continuously but only while there is traffic.

Verification
--------------
Both expressions were evaluated against the production Loki tenant on
2026-08-24, and both were shown to be able to FIRE rather than only to be
quiet:

  WitanCedarDenials     returns nothing as committed; the identical expression
                        with ``allowed="true"`` returned 5 change + 77 read
                        over the same 10-minute window.
  WitanGraphQuarantined returns a real 0 (both sides present, not an empty
                        result) with the 7-day baseline committed here; with
                        the baseline widened to 30 days it returns 1, correctly
                        catching the 17 -> 16 step described above.
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

# How long after a boot the comparison stays live. Outside this window the
# current-value side is empty and the rule evaluates to nothing.
_GRAPH_CURRENT_WINDOW = "6h"

_OMNIGRAPH_STREAM = 'namespace="omnigraph", container="omnigraph-server"'

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
    "omnigraph in {{ $labels.cluster }} came up serving fewer graphs than it did"
    " a week ago"
)

_QUARANTINE_DESCRIPTION = (
    "omnigraph-server in cluster {{ $labels.cluster }} started up serving fewer"
    " graphs than the most it has served in the past week."
    " OMNIGRAPH_REQUIRE_ALL_GRAPHS is deliberately unset, so a graph that"
    " cannot be opened is quarantined rather than fatal: the pod goes Ready,"
    " /healthz returns 200, and only requests against the missing graph fail."
    " If the missing graph is `council`, every agent on the service is broken"
    " and no other signal reports it. Run `kubectl -n omnigraph logs"
    " deploy/omnigraph-server | head -50` and compare the graphs it names"
    " against the declared list in cluster.yaml. If a graph was removed on"
    " purpose, silence this for a week while the baseline re-learns -- and"
    " confirm the removal really was intended, because a graph lost during a"
    " storage migration looks exactly the same from here."
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
    """Drop between the week's highest opened-graph count and the newest boot's.

    ``graph_count != ""`` selects only the one boot line that carries the
    field, so ``unwrap`` never sees a line it cannot parse.
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
        f"    min_over_time({stream} {parsed} [{_GRAPH_CURRENT_WINDOW}])\n"
        "  )\n"
        ") > 0"
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
        ],
        opts=resource_opts,
    )
