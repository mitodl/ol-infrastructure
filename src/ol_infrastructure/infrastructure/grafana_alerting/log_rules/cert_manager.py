"""cert-manager ACME and challenge log alert rules.

Source: grafana-alerts/loki-rules/cert-manager.yaml

Filters on cluster=~".*-production" / cluster!~".*-production" so each rule
fires on the appropriate stack and stays silent (no_data_state=OK) on others.

cert-manager emits a burst of ACME errors on pod startup while the issuer
cache initialises; the 20m for_ outlasts that window to avoid false pages.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create cert-manager log alert rule groups."""
    alerting.RuleGroup(
        "loki-cert-manager-production",
        name="cert-manager-production",
        folder_uid=folder_uid,
        interval_seconds=300,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="CertManagerACMEIssuerUnavailableProduction",
                condition="B",
                for_="20m",
                no_data_state="OK",
                labels={"severity": "critical", "environment": "production"},
                annotations={
                    "description": "cert-manager in {{ $labels.cluster }} cannot reach the ACME issuer outside of the expected pod startup initialization window. All certificate renewals are blocked until resolved.",
                    "resolution": "Check cert-manager pod logs and verify network connectivity to the ACME API. May indicate a network policy, DNS resolution failure, or a misconfigured ClusterIssuer credential.",
                },
                datas=rd(
                    "sum by (cluster) (\n"
                    "  count_over_time(\n"
                    '    {app_kubernetes_io_name="cert-manager", cluster=~".*-production"}\n'
                    '    |= "ACME client for issuer not initialised/available" [15m]\n'
                    "  )\n"
                    ") > 3"
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="CertManagerChallengePresentationFailureProduction",
                condition="B",
                for_="5m",
                no_data_state="OK",
                labels={"severity": "critical", "environment": "production"},
                annotations={
                    "description": "cert-manager in {{ $labels.cluster }} is failing to present ACME DNS-01 challenges. Certificate issuance will fail until resolved.",
                    "resolution": "Check IAM permissions for the Route 53 hosted zone used by the cert-manager challenge solver and verify the ClusterIssuer DNS provider configuration.",
                },
                datas=rd(
                    "sum by (cluster) (\n"
                    "  count_over_time(\n"
                    '    {app_kubernetes_io_name="cert-manager", cluster=~".*-production"}\n'
                    '    |= "error presenting challenge" [15m]\n'
                    "  )\n"
                    ") > 1"
                ),
            ),
        ],
        opts=resource_opts,
    )

    alerting.RuleGroup(
        "loki-cert-manager-non-production",
        name="cert-manager-non-production",
        folder_uid=folder_uid,
        interval_seconds=300,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="CertManagerACMEIssuerUnavailableNonProd",
                condition="B",
                for_="20m",
                no_data_state="OK",
                labels={"severity": "warning", "environment": "non-production"},
                annotations={
                    "description": "cert-manager in {{ $labels.cluster }} cannot reach the ACME issuer outside of the expected pod startup initialization window. Certificate renewals are blocked in this environment.",
                    "resolution": "Check cert-manager pod logs and verify network connectivity to the ACME API. May indicate a network policy, DNS resolution failure, or a misconfigured ClusterIssuer credential.",
                },
                datas=rd(
                    "sum by (cluster) (\n"
                    "  count_over_time(\n"
                    '    {app_kubernetes_io_name="cert-manager", cluster!~".*-production"}\n'
                    '    |= "ACME client for issuer not initialised/available" [15m]\n'
                    "  )\n"
                    ") > 3"
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="CertManagerChallengePresentationFailureNonProd",
                condition="B",
                for_="5m",
                no_data_state="OK",
                labels={"severity": "warning", "environment": "non-production"},
                annotations={
                    "description": "cert-manager in {{ $labels.cluster }} is failing to present ACME DNS-01 challenges. Certificate issuance will fail until resolved.",
                    "resolution": "Check IAM permissions for the Route 53 hosted zone used by the cert-manager challenge solver and verify the ClusterIssuer DNS provider configuration.",
                },
                datas=rd(
                    "sum by (cluster) (\n"
                    "  count_over_time(\n"
                    '    {app_kubernetes_io_name="cert-manager", cluster!~".*-production"}\n'
                    '    |= "error presenting challenge" [15m]\n'
                    "  )\n"
                    ") > 1"
                ),
            ),
        ],
        opts=resource_opts,
    )
