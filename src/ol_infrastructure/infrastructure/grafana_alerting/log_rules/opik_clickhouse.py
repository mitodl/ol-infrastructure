"""Opik backend queries that ClickHouse refused for the opik user.

Written 2026-11-04. ClickHouse refuses an opik query for two per-user reasons,
and opik-backend handles both the same way: the request returns 500, trace batch
writes included. The Opik SDK (2.2.94, rest_client_configurator/retry_decorator.py)
retries a 500 twice with 5-15 s backoff, then logs "Failed to process" and drops
the batch, so a refusal that lasts longer than that loses the traces.

- QUOTA_EXCEEDED (Code 201), from ``llmops_quota``. On 2026-11-03, in the hours
  ending 19:00 and 20:00 UTC, production opik-backend logged 2,332 and 3,505 of
  them (``queries = 20763/20000``), the only two hours with any in the 7 days to
  2026-11-04. Those counts used the narrower filter ``|= "QUOTA_EXCEEDED" |=
  "Causing: com.clickhouse.client.ClickHouseException"``. A quota refuses every
  query until its one-hour interval ends, well past the SDK's retries, so these
  are lost writes: Critical in production, Warning in CI and QA.
- TOO_MANY_SIMULTANEOUS_QUERIES (Code 202), from
  ``max_concurrent_queries_for_user`` in ``llmops_profile``. ClickHouse refuses
  the query rather than queueing it. Production opik-backend logged it on
  2026-11-04 at the old limit of 10 ("Current: 10, maximum: 10"). A short burst
  can clear within the SDK's retries, and the log line does not say whether the
  retry succeeded, so these are Warning in every environment: a pattern of them
  shows up without paging for a burst that lost nothing.

The rules match the error names rather than a full exception line, so a change
to opik-backend's exception wording cannot silence them; their threshold is > 0,
so the several lines logged per exception do not change when they fire.

Both limits are on the ``opik`` ClickHouse user, defined in
applications/clickhouse/__main__.py. The quota's query and error count caps were
removed after the 2026-11-03 incident and the concurrency limit was raised to 50;
the quota's hourly volume caps (result_rows, read_rows, execution_time) remain.
Matching the error code rather than one quota type covers those too, and any cap
reinstated later.

Why a log rule
--------------
Quota usage per user lives in ``system.quotas_usage``. The server's /metrics
serves metrics, events, asynchronous_metrics and errors, none of which is per
user or per quota; its error counters (``prometheus/errors``) count these errors
for the whole server, and were not checked for in Mimir when this was written.
opik-backend logs each refused query against the request that failed, which is
also how the incident was measured.

Routing
-------
``service="clickhouse"``, like every rule in metric_rules/clickhouse.py, so the
Grafana Production Service Route in saas/rootly attributes it to ``LLMOps -
ClickHouse``. The fix lives in the ClickHouse stack, not in Opik. Since
ol-infrastructure#6679 every environment's apps trace to production Opik, so CI
and QA traffic counts toward the production limits and the production rules are
the ones that matter.

for_="0m": the baseline is zero, and a quota refusal has already outlasted the
SDK's retries by the time an evaluation sees it, so a pending period would only
delay a page for data already lost. The 10-minute window is twice the 5-minute
evaluation interval, so a burst of refusals at the end of an interval is still
seen by one evaluation.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

_NON_PROD_CLUSTERS = ".*-(ci|qa)"
_PROD_CLUSTERS = ".*-(production)"
_LABELS = {"service": "clickhouse"}

# ClickHouse's error names for Code 201 and Code 202. Each appears in the
# server's message whichever quota type or limit value was hit.
_QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
_TOO_MANY_QUERIES = "TOO_MANY_SIMULTANEOUS_QUERIES"

_LIMITS_LOCATION = (
    " Both limits are in src/ol_infrastructure/applications/clickhouse/__main__.py:"
    " llmops_quota in _LLMOPS_QUOTAS (one-hour intervals ending on the hour) and"
    " max_concurrent_queries_for_user in _LLMOPS_PROFILES (per replica). Every"
    " environment's apps trace to production Opik, so look for the caller that"
    " drove the volume (UI sessions, online scoring, experiment and gate reads)"
    " before raising a limit."
)

_QUOTA_SUMMARY = (
    "Opik in {{ $labels.cluster }} logged {{ $values.B.Value }} ClickHouse"
    " quota refusal lines in 10 minutes"
)

_QUOTA_DESCRIPTION = (
    "opik-backend in namespace {{ $labels.namespace }} in cluster"
    " {{ $labels.cluster }} is logging ClickHouse Code 201 QUOTA_EXCEEDED for the"
    " opik user. The quota refuses every query until its interval ends, and the"
    " Opik SDK drops a trace or span batch after retrying for about 20 s, so"
    " traces are being lost. The log line names the limit and its usage, e.g."
    " 'queries = 20763/20000'. Current usage per replica is in"
    " system.quotas_usage (clickhouse-client as admin on a chi-clickhouse-* pod)."
    + _LIMITS_LOCATION
)

_CONCURRENCY_SUMMARY = (
    "Opik in {{ $labels.cluster }} logged {{ $values.B.Value }} ClickHouse"
    " concurrency refusal lines in 10 minutes"
)

_CONCURRENCY_DESCRIPTION = (
    "opik-backend in namespace {{ $labels.namespace }} in cluster"
    " {{ $labels.cluster }} is logging ClickHouse Code 202"
    " TOO_MANY_SIMULTANEOUS_QUERIES for the opik user (e.g. 'Current: 50,"
    " maximum: 50'). The Opik SDK retries a refused batch write twice within"
    " about 20 s, so a short burst may have lost nothing; repeated or sustained"
    " refusals mean writes are being dropped." + _LIMITS_LOCATION
)


def _refused_expr(error_name: str, cluster_filter: str) -> str:
    """Count opik-backend lines naming one ClickHouse refusal, per cluster, over 10m."""
    return (
        "sum by (cluster, namespace) (\n"
        "  count_over_time(\n"
        f'    {{namespace="opik", container="opik-backend", cluster=~"{cluster_filter}"}}\n'
        f'    |= "{error_name}" [10m]\n'
        "  )\n"
        ") > 0"
    )


def _rule(  # noqa: PLR0913
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    *,
    name: str,
    error_name: str,
    production: bool,
    severity: str,
    summary: str,
    description: str,
) -> alerting.RuleGroupRuleArgs:
    """Build one refusal rule for production or for CI and QA."""
    return alerting.RuleGroupRuleArgs(
        name=name,
        condition="C",
        for_="0m",
        no_data_state="OK",
        exec_err_state="KeepLast" if production else "OK",
        labels={
            **_LABELS,
            "severity": severity,
            "environment": "production" if production else "non-production",
        },
        annotations={"summary": summary, "description": description},
        datas=rd(
            _refused_expr(
                error_name, _PROD_CLUSTERS if production else _NON_PROD_CLUSTERS
            )
        ),
    )


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create the Opik ClickHouse refused-queries alert rule group."""
    alerting.RuleGroup(
        "loki-opik-clickhouse",
        name="opik-clickhouse",
        folder_uid=folder_uid,
        interval_seconds=300,
        rules=[
            _rule(
                rd,
                name="OpikClickHouseQuotaExceededWarning",
                error_name=_QUOTA_EXCEEDED,
                production=False,
                severity="warning",
                summary=_QUOTA_SUMMARY,
                description=_QUOTA_DESCRIPTION,
            ),
            _rule(
                rd,
                name="OpikClickHouseQuotaExceededCritical",
                error_name=_QUOTA_EXCEEDED,
                production=True,
                severity="critical",
                summary=_QUOTA_SUMMARY,
                description=_QUOTA_DESCRIPTION,
            ),
            _rule(
                rd,
                name="OpikClickHouseConcurrencyRefusedWarning",
                error_name=_TOO_MANY_QUERIES,
                production=False,
                severity="warning",
                summary=_CONCURRENCY_SUMMARY,
                description=_CONCURRENCY_DESCRIPTION,
            ),
            _rule(
                rd,
                name="OpikClickHouseConcurrencyRefusedProductionWarning",
                error_name=_TOO_MANY_QUERIES,
                production=True,
                severity="warning",
                summary=_CONCURRENCY_SUMMARY,
                description=_CONCURRENCY_DESCRIPTION,
            ),
        ],
        opts=resource_opts,
    )
