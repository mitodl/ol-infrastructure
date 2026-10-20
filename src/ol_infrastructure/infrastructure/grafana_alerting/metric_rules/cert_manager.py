"""cert-manager issuer and certificate state alert rules.

Replaces the CertManagerACMEIssuerUnavailable* log rules, which counted the
controller's "ACME client for issuer not initialised/available" line. The
controller logs a burst of that line on start, in the ~50ms before the issuer
cache populates, so two pod reschedules 15m apart (2026-10-20, applications-production)
kept the 15m count above threshold past the 20m for_ and paged with both
ClusterIssuers Ready and every certificate valid. These rules read the state
cert-manager reports about its own resources instead, so a restart cannot trip them.

Depends on the controller ServiceMonitor enabled in
infrastructure/aws/eks/cert_manager.py. Metric names and labels are from
cert-manager v1.21.2 (pkg/metrics/).

CertManagerCertificateRenewalOverdue keys off renewal_timestamp rather than a
fixed days-to-expiry threshold because renewBefore varies by issuer: the
Let's Encrypt certs renew 30d out of 90d, the aws-load-balancer serving certs
30d out of a year, the gravitino CA years out. Once a renewal succeeds the
timestamp moves into the future, so only a renewal that keeps failing stays past
due. Two days is several of cert-manager's backoff retries, and leaves weeks of
slack before a Let's Encrypt cert actually expires.

CertManagerMetricsMissing exists because every other rule here uses
no_data_state="OK": if the scrape breaks, they go quiet rather than fire.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

_ENVIRONMENTS = [
    # (rule name suffix, cluster selector, severity, environment, exec_err_state)
    ("Production", '".*-production"', "critical", "production", "KeepLast"),
    ("NonProd", '".*-(ci|qa)"', "warning", "non-production", "OK"),
]


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create cert-manager metric alert rule groups."""
    for suffix, cluster_re, severity, environment, exec_err_state in _ENVIRONMENTS:
        labels = {"severity": severity, "environment": environment}
        alerting.RuleGroup(
            f"cert-manager-{environment}",
            name=f"cert-manager-metrics-{environment}",
            folder_uid=folder_uid,
            interval_seconds=60,
            rules=[
                alerting.RuleGroupRuleArgs(
                    name=f"CertManagerClusterIssuerNotReady{suffix}",
                    condition="C",
                    for_="15m",
                    no_data_state="OK",
                    exec_err_state=exec_err_state,
                    labels=labels,
                    annotations={
                        "summary": "ClusterIssuer {{ $labels.name }} in {{ $labels.cluster }} is not Ready",
                        "description": "cert-manager reports ClusterIssuer {{ $labels.name }} in {{ $labels.cluster }} as not Ready. Certificates using it cannot be issued or renewed until it recovers.",
                        "resolution": "kubectl describe clusterissuer {{ $labels.name }} for the failing condition. For ACME issuers this is usually the account key Secret in the operations namespace, the ACME server URL, or egress to the ACME API.",
                    },
                    datas=rd(
                        "max by (cluster, name) ("
                        f'certmanager_clusterissuer_ready_status{{condition!="True", cluster=~{cluster_re}}}'
                        ") > 0"
                    ),
                ),
                alerting.RuleGroupRuleArgs(
                    name=f"CertManagerCertificateNotReady{suffix}",
                    condition="C",
                    for_="1h",
                    no_data_state="OK",
                    exec_err_state=exec_err_state,
                    labels=labels,
                    annotations={
                        "summary": "Certificate {{ $labels.namespace }}/{{ $labels.name }} in {{ $labels.cluster }} has not been Ready for 1h",
                        "description": "Certificate {{ $labels.namespace }}/{{ $labels.name }} (issuer {{ $labels.issuer_name }}) in {{ $labels.cluster }} has been not Ready for over an hour. A new certificate has never been issued, or an existing one's Secret no longer matches its spec.",
                        "resolution": "kubectl -n {{ $labels.namespace }} describe certificate {{ $labels.name }}, then follow the newest CertificateRequest, Order and Challenge. DNS-01 failures show up as CertManagerChallengePresentationFailure in the cert-manager log rules.",
                    },
                    datas=rd(
                        "max by (cluster, namespace, name, issuer_name) ("
                        f'certmanager_certificate_ready_status{{condition!="True", cluster=~{cluster_re}}}'
                        ") > 0"
                    ),
                ),
                alerting.RuleGroupRuleArgs(
                    name=f"CertManagerCertificateRenewalOverdue{suffix}",
                    condition="C",
                    for_="1h",
                    no_data_state="OK",
                    exec_err_state=exec_err_state,
                    labels=labels,
                    annotations={
                        "summary": "Certificate {{ $labels.namespace }}/{{ $labels.name }} in {{ $labels.cluster }} is more than 2 days past its renewal time",
                        "description": "Certificate {{ $labels.namespace }}/{{ $labels.name }} (issuer {{ $labels.issuer_name }}) in {{ $labels.cluster }} passed its renewal time over 2 days ago and has not been renewed. It keeps serving until it expires, but renewal is failing repeatedly.",
                        "resolution": "kubectl -n {{ $labels.namespace }} describe certificate {{ $labels.name }} and follow the newest CertificateRequest, Order and Challenge. Check CertManagerClusterIssuerNotReady for the same cluster first.",
                    },
                    datas=rd(
                        "count by (cluster, namespace, name, issuer_name) ("
                        f"time() - certmanager_certificate_renewal_timestamp_seconds{{cluster=~{cluster_re}}}"
                        " > 2 * 86400)"
                    ),
                ),
                # The left side is a positive series count, so the alert value is
                # never the 0 that base.py's Stage C would ignore.
                alerting.RuleGroupRuleArgs(
                    name=f"CertManagerMetricsMissing{suffix}",
                    condition="C",
                    for_="30m",
                    no_data_state="OK",
                    exec_err_state="OK",
                    labels={"severity": "warning", "environment": environment},
                    annotations={
                        "summary": "No cert-manager metrics from {{ $labels.cluster }}",
                        "description": "cert-manager is running in {{ $labels.cluster }} but none of its metrics have reached Mimir for 30m. The other cert-manager metric rules for this cluster cannot fire until this is fixed.",
                        "resolution": "Check that the cert-manager ServiceMonitor exists in the operations namespace and that the grafana-k8s-monitoring-alloy-metrics collector lists it as a target.",
                    },
                    datas=rd(
                        "count by (cluster) ("
                        f'kube_deployment_status_replicas_available{{deployment="cert-manager", cluster=~{cluster_re}}} > 0'
                        ") unless count by (cluster) ("
                        f"certmanager_clock_time_seconds_gauge{{cluster=~{cluster_re}}}"
                        ")"
                    ),
                ),
            ],
            opts=resource_opts,
        )
