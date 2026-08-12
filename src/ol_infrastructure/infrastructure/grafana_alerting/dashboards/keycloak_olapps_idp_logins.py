"""Per-identity-provider login dashboard for the olapps Keycloak realm.

Backed by the keycloak_olapps_idp_login_total /
keycloak_olapps_idp_login_failure_total Prometheus counters that Alloy
extracts from Keycloak's own login events (see substructure/aws/eks/grafana.py)
-- no login record, or the PII it carries, is shipped to Loki for this; only
the aggregate counters are.
"""

from collections.abc import Callable
from typing import Any

from pulumi import ResourceOptions


def _increase_expr(metric: str, window: str) -> str:
    """PromQL for "count of new increments within the window," robust to a
    counter appearing partway through it.

    Alloy's metric.counter is created lazily -- a per-IdP series only exists
    on /metrics once it's been incremented at least once. Prometheus's
    increase() computes a delta from a prior sample, so a series' very first
    appearance (right after any Alloy reload, or after being idle-evicted
    past max_idle_duration) is invisible to it: there's no earlier "0" sample
    to diff against, so it reports zero increase even though a login just
    happened. That's silent and looks like the pipeline is broken.

    `metric - metric offset window` sidesteps this: it's plain subtraction,
    not increase()'s reset-aware extrapolation. When the series existed
    `window` ago, this gives the correct in-window delta. When it didn't
    (the cold-start case), the offset lookup matches nothing, so the whole
    subtraction is absent for that series -- not zero -- letting `or metric`
    fall through to the counter's current raw value, which for a
    just-appeared series *is* the correct in-window count. clamp_min guards
    against a spurious negative from the rare case of a reset landing inside
    the window (max_idle_duration is long enough that this should be rare).
    """
    current = f'{metric}{{identity_provider!=""}}'
    return (
        f"round(clamp_min(sum by (identity_provider) ("
        f"({current} - {current} offset {window}) "
        f"or {current}"
        f"), 0))"
    )


def _dashboard_json(
    timeseries_panel: Callable[..., dict[str, Any]],
    bar_gauge_panel: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    return {
        "uid": "keycloak-olapps-idp-logins",
        "title": "Keycloak - olapps IdP Logins",
        "description": (
            "Per-identity-provider login counts for the olapps realm. "
            "Sourced from Prometheus counters Alloy extracts from Keycloak's "
            "own success/failure login events -- no login record, or the "
            "PII it carries, is shipped to Loki for this."
        ),
        "tags": ["keycloak", "olapps"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "time": {"from": "now-7d", "to": "now"},
        "refresh": "5m",
        "panels": [
            timeseries_panel(
                title="Successful logins by identity provider",
                expr=_increase_expr(
                    "loki_process_custom_keycloak_olapps_idp_login_total",
                    "$__interval",
                ),
                grid_pos={"h": 9, "w": 12, "x": 0, "y": 0},
            ),
            timeseries_panel(
                title="Failed logins by identity provider",
                expr=_increase_expr(
                    "loki_process_custom_keycloak_olapps_idp_login_failure_total",
                    "$__interval",
                ),
                grid_pos={"h": 9, "w": 12, "x": 12, "y": 0},
            ),
            bar_gauge_panel(
                title="Total successful logins (selected range)",
                expr=_increase_expr(
                    "loki_process_custom_keycloak_olapps_idp_login_total",
                    "$__range",
                ),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 9},
            ),
            bar_gauge_panel(
                title="Total failed logins (selected range)",
                expr=_increase_expr(
                    "loki_process_custom_keycloak_olapps_idp_login_failure_total",
                    "$__range",
                ),
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 9},
            ),
        ],
    }


def create(
    folder_uid,
    timeseries_panel: Callable[..., dict[str, Any]],
    bar_gauge_panel: Callable[..., dict[str, Any]],
    create_dashboard: Callable[[str, object, dict[str, Any], ResourceOptions], None],
    resource_opts: ResourceOptions,
) -> None:
    """Create the olapps per-IdP login dashboard."""
    create_dashboard(
        "keycloak-olapps-idp-logins-dashboard",
        folder_uid,
        _dashboard_json(timeseries_panel, bar_gauge_panel),
        resource_opts,
    )
