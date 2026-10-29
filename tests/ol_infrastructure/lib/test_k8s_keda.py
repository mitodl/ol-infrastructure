"""Tests for build_webapp_keda_config in ol_infrastructure.lib.k8s_keda."""

from typing import Any

from ol_infrastructure.lib.k8s_keda import build_webapp_keda_config

ROUTE_MATCHER = "app_route-production_.*"


def _trigger_queries(**kwargs: Any) -> list[str]:
    config = build_webapp_keda_config(
        trigger_auth_name="auth",
        route_matcher=ROUTE_MATCHER,
        container_name="app",
        **kwargs,
    )
    return [
        t["metadata"]["query"] for t in config.triggers if t["type"] == "prometheus"
    ]


def test_requests_rate_window_defaults_to_5m() -> None:
    """Callers that don't pass a window keep the query they had."""
    requests_query, _latency_query = _trigger_queries()
    assert requests_query == (
        f'sum(rate(apisix_http_status{{route=~"{ROUTE_MATCHER}"}}[5m]))'
    )


def test_requests_rate_window_override_leaves_latency_window_alone() -> None:
    """The window applies to the request-rate trigger only."""
    requests_query, latency_query = _trigger_queries(requests_rate_window="3m")
    assert requests_query == (
        f'sum(rate(apisix_http_status{{route=~"{ROUTE_MATCHER}"}}[3m]))'
    )
    assert "[5m]" in latency_query
