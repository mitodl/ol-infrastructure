"""Whether witan's stored memories reach the sessions they could help.

Nothing used to bring a memory into a session unless the agent decided to look
one up. Since witan-council added ``witan.memory.surfaced``, a first
``task_claim`` hands over the memories a task links to or its title matches.

The number to watch is "First claims that carried a memory". It stays low on
purpose: most tasks have nothing relevant, and a claim that shows an unrelated
memory teaches agents to skip the block, so the floors behind it
(``WITAN_RANK_SURFACE_MIN_*``) favour silence. Read it with the panel of recent
surfaced memories, because the share alone cannot say whether they were relevant.
It measures delivery, not reading: ``mcp.tool_call`` carries no slug, so nothing
here shows whether a surfaced memory was then opened.

"People reading memories" is the second number, and it is the reliable one for
reads. A "read" is a ``recall``, ``memory_search`` or ``memory_get``; a
``memory_list`` browse is left out. Raw read COUNTS are shown by tool for shape
only, because the web UI re-reads its open view every 30 seconds with those same
tools and nothing in the log line separates it from an agent, so one tab left
open on a memory can outnumber a week of agents. That is also why there is no
reads-per-write ratio here. The 7 days to 2026-11-02, before claims surfaced
anything: 64 reads by 6 people, against 10 memories stored.

Every panel reads Loki, not ``witan_tool_calls_total``, which undercounts rare
tools and memory tools are the rare ones: over those 7 days it read 6 ``recall``
calls against 13 in the logs, and 3,511 ``task_ready`` against 3,517.
``mcp.tool_call`` is the structured line witan's middleware writes for every
call, in the ``mcp`` container (``witan-server`` is the app label, and selecting
on it as a container returns a confident zero).

The panels on ``witan.memory.surfaced`` read "No data" until a witan-council with
that event is deployed, which is what No data means here: the share is
zero-filled so that claims carrying nothing read 0%.
"""

from collections.abc import Callable
from typing import Any

from pulumi import Input, ResourceOptions

from ol_infrastructure.infrastructure.grafana_alerting.dashboards.datasources import (
    LOKI_DATASOURCE_REF,
)
from ol_infrastructure.infrastructure.grafana_alerting.dashboards.promql import ZERO

_SELECTOR = '{namespace="witan", container="mcp"}'
_TOOL_CALLS = f'{_SELECTOR} |= "mcp.tool_call" | json | outcome="ok"'
_READS = f'{_TOOL_CALLS} | tool=~"recall|memory_search|memory_get"'
_STORES = f'{_TOOL_CALLS} | tool="memory_store"'
_SURFACED = f'{_SELECTOR} |= "witan.memory.surfaced" | json'

# A `[1d]` window at Grafana's sub-hour step overlaps itself, so the default
# `sum` legend would add each read many times over: about 5,000 for a 70-read
# week. See "Legend" in AGENTS.md.
_TRAILING_DAY_LEGEND = "lastNotNull"


def _dashboard_json(
    timeseries_panel: Callable[..., dict[str, Any]],
    stat_panel: Callable[..., dict[str, Any]],
    logs_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    return {
        "uid": "witan-memory",
        "title": "Witan - Memory Reach",
        "description": (
            "Whether stored memories reach sessions: how often a first task "
            "claim carried one, and how many people read them unasked."
        ),
        "tags": ["witan"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "time": {"from": "now-7d", "to": "now"},
        "refresh": "1h",
        "panels": [
            row_panel(title="Reaching sessions", y=0),
            stat_panel(
                title="First claims that carried a memory",
                expr=(
                    "(sum(count_over_time("
                    f'{_SURFACED} | count != "0" [$__range])) {ZERO})'
                    f" / sum(count_over_time({_SURFACED} [$__range]))"
                ),
                grid_pos={"h": 5, "w": 6, "x": 0, "y": 1},
                datasource_ref=LOKI_DATASOURCE_REF,
                unit="percentunit",
                decimals=0,
            ),
            stat_panel(
                title="Memories surfaced",
                expr=f"sum(sum_over_time({_SURFACED} | unwrap count [$__range]))",
                grid_pos={"h": 5, "w": 6, "x": 6, "y": 1},
                datasource_ref=LOKI_DATASOURCE_REF,
            ),
            stat_panel(
                title="People reading memories",
                expr=f"count(sum by (actor) (count_over_time({_READS} [$__range])))",
                grid_pos={"h": 5, "w": 6, "x": 12, "y": 1},
                datasource_ref=LOKI_DATASOURCE_REF,
            ),
            stat_panel(
                title="Memories stored",
                expr=f"sum(count_over_time({_STORES} [$__range]))",
                grid_pos={"h": 5, "w": 6, "x": 18, "y": 1},
                datasource_ref=LOKI_DATASOURCE_REF,
            ),
            row_panel(title="Over time", y=6),
            timeseries_panel(
                title="Memory reads by tool, trailing day (includes web UI polling)",
                expr=f"sum by (tool) (count_over_time({_READS} [1d]))",
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 7},
                datasource_ref=LOKI_DATASOURCE_REF,
                legend_format="{{tool}}",
                legend_calc=_TRAILING_DAY_LEGEND,
            ),
            timeseries_panel(
                title="Reaching sessions, trailing day",
                queries=[
                    {
                        "expr": (
                            f"sum(sum_over_time({_SURFACED} | unwrap count [1d]))"
                        ),
                        "legend_format": "memories surfaced",
                    },
                    {
                        "expr": f"sum(count_over_time({_STORES} [1d]))",
                        "legend_format": "memories stored",
                    },
                ],
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 7},
                datasource_ref=LOKI_DATASOURCE_REF,
                legend_calc=_TRAILING_DAY_LEGEND,
            ),
            row_panel(title="Were they relevant?", y=15),
            logs_panel(
                title="Recently surfaced memories, by task",
                expr=f'{_SURFACED} | count != "0"',
                grid_pos={"h": 10, "w": 24, "x": 0, "y": 16},
            ),
        ],
    }


def create(
    folder_uid: Input[str],
    timeseries_panel: Callable[..., dict[str, Any]],
    stat_panel: Callable[..., dict[str, Any]],
    logs_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
    create_dashboard: Callable[
        [str, Input[str], dict[str, Any], ResourceOptions], None
    ],
    resource_opts: ResourceOptions,
) -> None:
    """Create the witan memory-reach dashboard."""
    create_dashboard(
        "witan-memory-dashboard",
        folder_uid,
        _dashboard_json(timeseries_panel, stat_panel, logs_panel, row_panel),
        resource_opts,
    )
