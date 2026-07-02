"""MIT Learn 5xx error rate log alert rules.

Source: grafana-alerts/loki-rules/mit-learn.yaml

These rules target cluster="applications-production" so they only fire on the
production Loki tenant and stay silent (no_data_state=OK) on CI/QA stacks.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create MIT Learn 5xx error rate log alert rule groups."""
    alerting.RuleGroup(
        "loki-mit-learn-5xx-error-percentage",
        name="5xx-error-percentage",
        folder_uid=folder_uid,
        interval_seconds=300,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="mitxonline-5xx-error-percentage",
                condition="B",
                for_="5m",
                no_data_state="OK",
                labels={"severity": "critical", "service": "mitxonline"},
                annotations={
                    "description": "An increase in 5xx errors that may indicate an issue with mitxonline",
                },
                datas=rd(
                    'sum(rate({cluster="applications-production", namespace="mitxonline", container="nginx"}'
                    ' | pattern `<client> - - [<_>] "<method> <uri> <_>" <status> <bytes> "<referer>" "<agent>" "<_>"`'
                    ' | status=~"5.." [5m]))'
                    " / "
                    'sum(rate({cluster="applications-production", namespace="mitxonline", container="nginx"}'
                    ' | pattern `<client> - - [<_>] "<method> <uri> <_>" <status> <bytes> "<referer>" "<agent>" "<_>"`'
                    " [5m])) * 100 > 5"
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="mitlearn-5xx-error-percentage",
                condition="B",
                for_="5m",
                no_data_state="OK",
                labels={"severity": "critical", "service": "mitlearn"},
                annotations={
                    "description": "An increase in 5xx errors that may indicate an issue with mitlearn",
                },
                datas=rd(
                    'sum(rate({cluster="applications-production", namespace="mitlearn", container="nginx"}'
                    ' | pattern `<client> - - [<_>] "<method> <uri> <_>" <status> <bytes> "<referer>" "<agent>" "<_>"`'
                    ' | status=~"5.." [5m]))'
                    " / "
                    'sum(rate({cluster="applications-production", namespace="mitlearn", container="nginx"}'
                    ' | pattern `<client> - - [<_>] "<method> <uri> <_>" <status> <bytes> "<referer>" "<agent>" "<_>"`'
                    " [5m])) * 100 > 5"
                ),
            ),
        ],
        opts=resource_opts,
    )
