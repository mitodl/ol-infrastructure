"""Per-graph growth of the version objects omnigraph cleanup has to plan.

Written 2026-10-28. omnigraph 0.13's ``cleanup`` reads every object under a
graph's ``_versions/`` directories before it deletes anything, 0.16 to 0.22s
each on S3, and it never deletes a ``__manifest`` version
(ModernRelay/omnigraph#644, #1095). A cleanup whose plan outlasts its command
timeout deletes nothing. QA and Production set
``omnigraph:cleanup_iso_weekdays: []`` (ol-infrastructure #6520, #6522), so
nothing cleans them on a schedule and nothing else reports how far behind
they are.

The counts come from the ``omnigraph-version-census`` CronJob
(applications/omnigraph/version_census.py), which lists the store hourly and
prints one logfmt line per graph::

    event=omnigraph_versions graph=council manifest_versions=4680
    table_versions=4645 versions=9325

(wrapped here; it is one line, with no ANSI escapes, so it needs no
``decolorize``).

Measured 2026-10-28 from the same listing the CronJob makes
------------------------------------------------------------
Production: 42,354 versions on 18 graphs (15,832 manifest, 26,522 table). The
largest are council 9,325, ol-data-platform 8,108, ol-infrastructure 6,990
and code-bridge 5,289. Created per graph per day over 2026-10-22 to 10-27:
council up to 1,454, ol-data-platform up to 1,230, and one day of 2,739 on
open-edx-plugins (10-27), the largest single-graph day seen. Weekends are an
order of magnitude lower.

QA: 8,540 versions. code-bridge is the largest at 2,550 and added 490 to 570
a day over 2026-10-24 to 10-27.

Thresholds
----------
``OmnigraphCleanupPlanOverBudget*``: a graph whose projected cleanup plan,
``versions`` x 0.22s, passes an hour. 0.22s is the slow end of the measured
range (Production's 2026-10-20 backlog run; QA's 2026-10-21 nightly), so the
projection errs long. An hour is 16,364 versions. Cleanup stops the whole data
tier, so this is one graph keeping every witan user offline for an hour in a
planned run, and twice the runbook's 1,800s per-command timeout for clearing
a backlog. Past it a long run stops being a reasonable way to clean that graph,
and the choice is between rebuilding it and waiting for upstream
(tk-decide-how-to-bound-omnigraph-cleanup-planning-w-d9fa8f). On the
2026-10-28 numbers council is at 2,050s and gained about 800 versions a day
averaged over 2026-10-22 to 10-27, so at that rate it fires in Production
around 2026-11-06, and ol-data-platform (about 770 a day) around 11-08. That firing is the signal this
exists to give, not noise to tune away.

``OmnigraphVersionGrowth*``: a graph that added more than 5,000 versions in 24
hours, about 1,100s of cleanup plan in a day. That is 1.8x the largest
single-graph day measured, and 3.4x council's largest. It catches a writer
committing in a loop, which would otherwise show only as the budget rule
firing days early.

The two compare different things: one the level, the other the slope. A graph
can trip the growth rule long before the budget rule, and the budget rule
keeps firing on steady growth that never trips the growth rule.

Shape
-----
Both are ``max by (cluster, graph)`` over ``max_over_time`` of the unwrapped
count. ``logfmt`` extracts every field as a label, so the inner aggregation
carries one series per distinct value set, and the outer ``max by`` folds them
back to one per graph. The 2h range keeps a sample through one missed hourly
run. It is the highest count in the window, not the newest, so after a cleanup
the budget rule reads the pre-cleanup count for up to 2h. The growth rule subtracts the same expression ``offset 1d``. A
graph with no sample a day earlier has no match on the right and stays silent,
and a cleanup makes the difference negative. Both comparisons leave the
positive left-hand value on the row, which is what base.py's ``last(A) > 0``
needs: the projected seconds for one, the versions added for the other.

Both shapes were evaluated against the production Loki tenant on 2026-10-28
with the CI indexer's ``scanned=`` count standing in for ``versions`` (no
census line existed yet). The budget shape returned 489.94 (2,227 x 0.22) and
the growth shape returned 8, each labelled ``cluster`` and ``graph``.

Routing
-------
``channel=devops-warnings``, warning, in both environments. A growing backlog
is a capacity fact, not an outage: it wants a decision within days, not a
page. ``graph`` is in the notification policy's ``group_bies``, so two graphs
crossing at once arrive as two notifications.

NOT covered here: the census not running. No lines is NoData, which is OK.
``omnigraph-version-census`` is in eks_general.py's fast (6h) staleness
bucket for that, and a failed run is ``WorkloadJobFailed*``'s.

QA and Production only: operations-ci ships no logs, so CI's Loki tenant has
no census lines.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

from ol_infrastructure.lib.pulumi_helper import parse_stack

_NON_PROD_CLUSTERS = ".*-(ci|qa)"
_PROD_CLUSTERS = ".*-(production)"

_CENSUS_STREAM = 'namespace="omnigraph", container="count-versions"'
_CENSUS_EVENT = "event=omnigraph_versions"

# The slow end of the measured per-object plan cost on S3.
_PLAN_SECONDS_PER_VERSION = 0.22
_PLAN_BUDGET_SECONDS = 3600
_DAILY_GROWTH_VERSIONS = 5000

# Long enough to keep an hourly sample through one missed run.
_SAMPLE_WINDOW = "2h"

_ROUTING = {"channel": "devops-warnings", "severity": "warning"}

_BUDGET_SUMMARY = (
    "omnigraph graph {{ $labels.graph }} in {{ $labels.cluster }} would take"
    " over an hour to plan a cleanup"
)

_BUDGET_DESCRIPTION = (
    "Graph {{ $labels.graph }} in cluster {{ $labels.cluster }} has enough"
    " `_versions/` objects that `omnigraph cleanup` would spend about"
    " {{ $values.B.Value }}s planning before it deleted anything (0.22s per object,"
    " the slow end of what Production and QA measured). The server is stopped"
    " for that whole time, and a plan that outlasts its command timeout"
    " deletes nothing. Nothing schedules cleanup in QA or Production"
    " (omnigraph:cleanup_iso_weekdays: []), and omnigraph never deletes"
    " `__manifest` versions (ModernRelay/omnigraph#644), so this only grows."
    " Read the counts with"
    ' `{namespace="omnigraph", container="count-versions"} |='
    ' "graph={{ $labels.graph }} "`: manifest_versions cannot be reduced by any'
    " cleanup, table_versions can. A derived code-* graph can be rebuilt by"
    " dropping it and reindexing. For council, see"
    " tk-decide-how-to-bound-omnigraph-cleanup-planning-w-d9fa8f and"
    ' docs/omnigraph-store-maintenance-runbook.md ("Clearing a cleanup'
    ' backlog") before planning a long run.'
)

_GROWTH_SUMMARY = (
    "omnigraph graph {{ $labels.graph }} in {{ $labels.cluster }} added over"
    " 5,000 versions in a day"
)

_GROWTH_DESCRIPTION = (
    "Graph {{ $labels.graph }} in cluster {{ $labels.cluster }} gained"
    " {{ $values.B.Value }} `_versions/` objects in the last 24 hours, about 1,100s of"
    " cleanup planning in one day. The largest single-graph day measured"
    " before this rule was 2,739 (open-edx-plugins in Production,"
    " 2026-10-27). Every graph commit adds one manifest version plus one per"
    " table it touched, so this is a writer committing far more often than"
    " usual: a CI indexer loop, a witan-code client re-indexing on every"
    " save, or a script. Compare the hourly counts with"
    ' `{namespace="omnigraph", container="count-versions"} |='
    ' "graph={{ $labels.graph }} "` to see when it started, then the'
    " writers' own logs (witan-ci-indexer in namespace witan, and"
    ' `{namespace="omnigraph", container="omnigraph-server"} | decolorize |'
    " logfmt` for actor_id on that graph's writes)."
)


def _recent_max_versions(cluster_filter: str, offset: str = "") -> str:
    """Return the highest ``versions`` count per graph over the sample window."""
    offset_clause = f" offset {offset}" if offset else ""
    return (
        "max by (cluster, graph) (\n"
        "  max_over_time(\n"
        f'    {{{_CENSUS_STREAM}, cluster=~"{cluster_filter}"}}\n'
        f'    |= "{_CENSUS_EVENT}" | logfmt | __error__=""\n'
        f"    | unwrap versions [{_SAMPLE_WINDOW}]{offset_clause}\n"
        "  )\n"
        ")"
    )


def _plan_budget_expr(cluster_filter: str) -> str:
    """Projected cleanup plan seconds per graph, where over budget."""
    return (
        f"{_recent_max_versions(cluster_filter)}\n"
        f"* {_PLAN_SECONDS_PER_VERSION} > {_PLAN_BUDGET_SECONDS}"
    )


def _daily_growth_expr(cluster_filter: str) -> str:
    """Versions added per graph over 24h, where above the growth threshold."""
    return (
        "(\n"
        f"{_recent_max_versions(cluster_filter)}\n"
        "-\n"
        f"{_recent_max_versions(cluster_filter, offset='1d')}\n"
        f") > {_DAILY_GROWTH_VERSIONS}"
    )


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create the omnigraph version-growth rule group, on QA and Production only."""
    if parse_stack().env_suffix == "ci":
        return

    alerting.RuleGroup(
        "loki-omnigraph-versions",
        name="omnigraph-versions",
        folder_uid=folder_uid,
        interval_seconds=300,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="OmnigraphCleanupPlanOverBudgetWarning",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={**_ROUTING, "environment": "non-production"},
                annotations={
                    "summary": _BUDGET_SUMMARY,
                    "description": _BUDGET_DESCRIPTION,
                },
                datas=rd(_plan_budget_expr(_NON_PROD_CLUSTERS)),
            ),
            alerting.RuleGroupRuleArgs(
                name="OmnigraphCleanupPlanOverBudgetProdWarning",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={**_ROUTING, "environment": "production"},
                annotations={
                    "summary": _BUDGET_SUMMARY,
                    "description": _BUDGET_DESCRIPTION,
                },
                datas=rd(_plan_budget_expr(_PROD_CLUSTERS)),
            ),
            alerting.RuleGroupRuleArgs(
                name="OmnigraphVersionGrowthWarning",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={**_ROUTING, "environment": "non-production"},
                annotations={
                    "summary": _GROWTH_SUMMARY,
                    "description": _GROWTH_DESCRIPTION,
                },
                datas=rd(_daily_growth_expr(_NON_PROD_CLUSTERS)),
            ),
            alerting.RuleGroupRuleArgs(
                name="OmnigraphVersionGrowthProdWarning",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={**_ROUTING, "environment": "production"},
                annotations={
                    "summary": _GROWTH_SUMMARY,
                    "description": _GROWTH_DESCRIPTION,
                },
                datas=rd(_daily_growth_expr(_PROD_CLUSTERS)),
            ),
        ],
        opts=resource_opts,
    )
