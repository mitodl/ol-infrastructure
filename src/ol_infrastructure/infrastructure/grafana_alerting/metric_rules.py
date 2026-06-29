"""Grafana metric alert rule groups.

Migrated from grafana-alerts/cortex-rules/ (previously synced via cortextool).
Each YAML file maps to one or more Grafana RuleGroup resources, preserving the
original group names and evaluation intervals.

Two-stage evaluation pipeline per rule
---------------------------------------
Grafana-managed alert rules cannot use a bare PromQL expression as a condition
the way the Mimir ruler API can. Instead each rule needs:

  Stage A — instant PromQL query against the per-environment Mimir datasource.
             The PromQL already contains the threshold (e.g. "< 1.0"), so it
             returns a non-empty result set only when the condition is met.
  Stage B — classic condition: fires when the count of rows returned by A > 0.

The condition field on each rule points to "B", meaning "alert when B fires".

no_data_state="OK"
-------------------
Every EKS rule has a cluster label selector baked into its PromQL expression
(e.g. cluster=~".*-(ci|qa)").  Each Grafana Cloud stack queries its own Mimir
tenant, which only has metrics from the matching environment's clusters.  When
the selector returns no data (e.g. the production filter on the CI stack),
no_data_state="OK" keeps the rule silent instead of surfacing a NoData alert.

Sources
-------
  grafana-alerts/cortex-rules/eks_general.yaml   → eks-general rule group
  grafana-alerts/cortex-rules/linux-host.yaml    → linux-host-* rule groups
"""

import json

from pulumi import ResourceOptions
from pulumiverse_grafana import alerting
from pulumiverse_grafana.oss.folder import Folder

from ol_infrastructure.lib.pulumi_helper import StackInfo

# Mimir is exposed in each Grafana Cloud stack as a Prometheus-compatible
# datasource. These UIDs are not sensitive — they are the stable identifiers
# visible in Grafana UI under Configuration → Data Sources.
_MIMIR_DATASOURCE_UID: dict[str, str] = {
    "ci": "grafanacloud-mitolci-prom",
    "qa": "grafanacloud-mitolqa-prom",
    "production": "grafanacloud-mitolproduction-prom",
}


def _rule_data(expr: str, mimir_uid: str) -> list[alerting.RuleGroupRuleDataArgs]:
    """Build the two-stage data pipeline for a single alert rule."""
    return [
        # Stage A: run the PromQL expression against Mimir.
        alerting.RuleGroupRuleDataArgs(
            ref_id="A",
            datasource_uid=mimir_uid,
            relative_time_range=alerting.RuleGroupRuleDataRelativeTimeRangeArgs(
                from_=600, to=0
            ),
            model=json.dumps(
                {
                    "datasource": {"type": "prometheus", "uid": mimir_uid},
                    "expr": expr,
                    "instant": True,
                    "intervalMs": 1000,
                    "maxDataPoints": 43200,
                    "refId": "A",
                }
            ),
        ),
        # Stage B: fire when A returns any rows (count > 0).
        alerting.RuleGroupRuleDataArgs(
            ref_id="B",
            datasource_uid="-100",  # "-100" is Grafana's internal UID for expressions
            relative_time_range=alerting.RuleGroupRuleDataRelativeTimeRangeArgs(
                from_=600, to=0
            ),
            model=json.dumps(
                {
                    "conditions": [
                        {
                            "evaluator": {"type": "gt", "params": [0]},
                            "operator": {"type": "and"},
                            "query": {"params": ["A"]},
                            "reducer": {"type": "count", "params": []},
                            "type": "query",
                        }
                    ],
                    "datasource": {"type": "__expr__", "uid": "-100"},
                    "refId": "B",
                    "type": "classic_conditions",
                }
            ),
        ),
    ]


def create(stack_info: StackInfo, resource_opts: ResourceOptions) -> None:
    """Create Grafana alert rule groups for EKS and Linux host metrics."""
    mimir_uid = _MIMIR_DATASOURCE_UID[stack_info.env_suffix]

    # Folder that hosts all infrastructure alert rule groups.
    # The uid is stable so Pulumi can track it across runs.
    alerts_folder = Folder(
        "infrastructure-alerts-folder",
        title="Infrastructure Alerts",
        uid="infrastructure-alerts",
        opts=resource_opts,
    )

    def rd(expr: str) -> list[alerting.RuleGroupRuleDataArgs]:
        return _rule_data(expr, mimir_uid)

    # -------------------------------------------------------------------------
    # EKS general rules
    # Source: grafana-alerts/cortex-rules/eks_general.yaml
    #
    # Warning rules filter cluster=~".*-(ci|qa)"   — fire on CI and QA stacks.
    # Critical rules filter cluster=~".*-(production)" — fire on prod stack only.
    # Rules with no matching data on a given stack stay silent (no_data_state=OK).
    # -------------------------------------------------------------------------
    alerting.RuleGroup(
        "eks-general",
        name="general",
        folder_uid=alerts_folder.uid,
        interval_seconds=60,
        rules=[
            # --- Daemonset replicas ---
            # Fires when scheduled replicas / desired replicas < 1.0, meaning at
            # least one node is missing a daemonset pod (e.g. scheduling issue).
            alerting.RuleGroupRuleArgs(
                name="DaemonsetReplicasMissingWarning",
                condition="B",
                for_="10m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "There is a mismatch between the requested number of instances for daemonset {{ $labels.daemonset }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }}. This may mean there is a node stuck leaving or joining the cluster or another issue preventing the daemonset from being correctly scheduled."
                },
                datas=rd(
                    'sum by (cluster, namespace, daemonset) (kube_daemonset_status_current_number_scheduled{cluster=~".*-(ci|qa)"}) / sum by (cluster, namespace, daemonset) (kube_daemonset_status_desired_number_scheduled) < 1.0'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="DaemonsetReplicasMissingCritical",
                condition="B",
                for_="10m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "There is a mismatch between the requested number of instances for daemonset {{ $labels.daemonset }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }}. This may mean there is a node stuck leaving or joining the cluster or another issue preventing the daemonset from being correctly scheduled."
                },
                datas=rd(
                    'sum by (cluster, namespace, daemonset) (kube_daemonset_status_current_number_scheduled{cluster=~".*-(production)"}) / sum by (cluster, namespace, daemonset) (kube_daemonset_status_desired_number_scheduled) < 1.0'
                ),
            ),
            # --- Deployment replicas ---
            # Fires when available replicas / total replicas < 1.0.
            alerting.RuleGroupRuleArgs(
                name="DeploymentReplicasMissingWarning",
                condition="B",
                for_="10m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "There is a mismatch between the requested number of instances for deployment {{ $labels.deployment }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }}."
                },
                datas=rd(
                    'sum by (cluster, namespace, deployment) (kube_deployment_status_replicas_available{cluster=~".*-(ci|qa)"}) / sum by (cluster, namespace, deployment) (kube_deployment_status_replicas) < 1.0'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="DeploymentReplicasMissingCritical",
                condition="B",
                for_="10m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "There is a mismatch between the requested number of instances for deployment {{ $labels.deployment }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }}."
                },
                datas=rd(
                    'sum by (cluster, namespace, deployment) (kube_deployment_status_replicas_available{cluster=~".*-(production)"}) / sum by (cluster, namespace, deployment) (kube_deployment_status_replicas) < 1.0'
                ),
            ),
            # --- Deployment availability ---
            # Fires when the Available condition on a deployment is "false",
            # meaning the deployment cannot serve traffic.
            alerting.RuleGroupRuleArgs(
                name="DeploymentUnavailableWarning",
                condition="B",
                for_="10m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "A deployment {{ $labels.deployment }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} is not available for an extended period of time."
                },
                datas=rd(
                    'sum by (cluster, namespace, deployment, condition, status) (kube_deployment_status_condition{cluster=~".*-(ci|qa)", condition="Available", status="false"}) > 0'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="DeploymentUnavailableCritical",
                condition="B",
                for_="10m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "A deployment {{ $labels.deployment }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} is not available for an extended period of time."
                },
                datas=rd(
                    'sum by (cluster, namespace, deployment, condition, status) (kube_deployment_status_condition{cluster=~".*-(production)", condition="Available", status="false"}) > 0'
                ),
            ),
            # --- StatefulSet replicas ---
            # Fires when ready replicas / desired replicas < 1.0.
            alerting.RuleGroupRuleArgs(
                name="StatefulSetReplicasMissingWarning",
                condition="B",
                for_="10m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "There is a mismatch between the requested number of instances for statefulset {{ $labels.statefulset }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }}."
                },
                datas=rd(
                    'sum by (cluster, namespace, statefulset) (kube_statefulset_status_replicas_ready{cluster=~".*-(ci|qa)"}) / sum by (cluster, namespace, statefulset) (kube_statefulset_replicas) < 1.0'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="StatefulSetReplicasMissingCritical",
                condition="B",
                for_="10m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "There is a mismatch between the requested number of instances for statefulset {{ $labels.statefulset }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }}."
                },
                datas=rd(
                    'sum by (cluster, namespace, statefulset) (kube_statefulset_status_replicas_ready{cluster=~".*-(production)"}) / sum by (cluster, namespace, statefulset) (kube_statefulset_replicas) < 1.0'
                ),
            ),
            # --- Node readiness ---
            # Fires when a node's Ready condition == 0 (not ready).
            alerting.RuleGroupRuleArgs(
                name="NodeNotReadyWarning",
                condition="B",
                for_="5m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "Node {{ $labels.node }} in cluster {{ $labels.cluster }} has been in a not-ready state for more than 5 minutes."
                },
                datas=rd(
                    'sum by (cluster, node) (kube_node_status_condition{cluster=~".*-(ci|qa)", condition="Ready", status="true"} == 0)'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="NodeNotReadyCritical",
                condition="B",
                for_="5m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "Node {{ $labels.node }} in cluster {{ $labels.cluster }} has been in a not-ready state for more than 5 minutes."
                },
                datas=rd(
                    'sum by (cluster, node) (kube_node_status_condition{cluster=~".*-(production)", condition="Ready", status="true"} == 0)'
                ),
            ),
            # --- Pod crash looping ---
            # Fires when a container is in CrashLoopBackOff state.
            alerting.RuleGroupRuleArgs(
                name="PodCrashLoopingWarning",
                condition="B",
                for_="5m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "Container {{ $labels.container }} in pod {{ $labels.pod }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} is in CrashLoopBackOff."
                },
                datas=rd(
                    'sum by (cluster, namespace, pod, container) (kube_pod_container_status_waiting_reason{cluster=~".*-(ci|qa)", reason="CrashLoopBackOff"}) > 0'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="PodCrashLoopingCritical",
                condition="B",
                for_="5m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "Container {{ $labels.container }} in pod {{ $labels.pod }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} is in CrashLoopBackOff."
                },
                datas=rd(
                    'sum by (cluster, namespace, pod, container) (kube_pod_container_status_waiting_reason{cluster=~".*-(production)", reason="CrashLoopBackOff"}) > 0'
                ),
            ),
            # --- Celery Beat restarts ---
            # Fires when a celery-beat pod restarts more than 3 times in 1 hour,
            # which indicates OOM kills or crash loops in the scheduler process.
            # Runbook: https://github.com/mitodl/mit-learn/wiki/Celery-Beat-Troubleshooting
            alerting.RuleGroupRuleArgs(
                name="CeleryBeatPodRestarts",
                condition="B",
                for_="5m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "summary": "Celery Beat pod {{ $labels.pod }} in namespace {{ $labels.namespace }} has restarted {{ $value }} times in the last hour",
                    "description": "High restart count suggests OOM kills or crash loops. Verify memory allocation, pod memory requests/limits, and actual usage.",
                    "runbook_url": "https://github.com/mitodl/mit-learn/wiki/Celery-Beat-Troubleshooting",
                },
                datas=rd(
                    'increase(kube_pod_container_status_restarts_total{cluster=~".*-(ci|qa)", pod=~".*celery-beat.*|.*celerybeat.*"}[1h]) > 3'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="CeleryBeatPodRestartsCritical",
                condition="B",
                for_="5m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "summary": "Celery Beat pod {{ $labels.pod }} in namespace {{ $labels.namespace }} has restarted {{ $value }} times in the last hour",
                    "description": "High restart count suggests OOM kills or crash loops. Verify memory allocation, pod memory requests/limits, and actual usage.",
                    "runbook_url": "https://github.com/mitodl/mit-learn/wiki/Celery-Beat-Troubleshooting",
                },
                datas=rd(
                    'increase(kube_pod_container_status_restarts_total{cluster=~".*-(production)", pod=~".*celery-beat.*|.*celerybeat.*"}[1h]) > 3'
                ),
            ),
            # --- OOM kills ---
            # Fires when a container was last terminated by OOMKilled AND is
            # actively restarting (restart count increased in the past hour).
            # The join ensures we only alert on OOM-killed containers that are
            # still looping, not historical one-off kills.
            alerting.RuleGroupRuleArgs(
                name="PodOOMKilledWarning",
                condition="B",
                for_="5m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "Container {{ $labels.container }} in pod {{ $labels.pod }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has been OOMKilled and is actively restarting. Memory limits may need to be increased."
                },
                datas=rd(
                    "sum by (cluster, namespace, pod, container) (\n"
                    '  (kube_pod_container_status_last_terminated_reason{cluster=~".*-(ci|qa)", reason="OOMKilled"} == 1)\n'
                    "  * on (cluster, namespace, pod, container) group_left()\n"
                    "  (increase(kube_pod_container_status_restarts_total[1h]) > 0)\n"
                    ")"
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="PodOOMKilledCritical",
                condition="B",
                for_="5m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "Container {{ $labels.container }} in pod {{ $labels.pod }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has been OOMKilled and is actively restarting. Memory limits may need to be increased."
                },
                datas=rd(
                    "sum by (cluster, namespace, pod, container) (\n"
                    '  (kube_pod_container_status_last_terminated_reason{cluster=~".*-(production)", reason="OOMKilled"} == 1)\n'
                    "  * on (cluster, namespace, pod, container) group_left()\n"
                    "  (increase(kube_pod_container_status_restarts_total[1h]) > 0)\n"
                    ")"
                ),
            ),
            # --- Kubernetes Job failures ---
            # Fires when a Job's failed pod count > 0.
            # Dagster namespace is excluded — it manages its own job retry logic.
            alerting.RuleGroupRuleArgs(
                name="KubernetesJobFailedWarning",
                condition="B",
                for_="5m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "Job {{ $labels.job_name }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has failed."
                },
                datas=rd(
                    'sum by (cluster, namespace, job_name) (kube_job_status_failed{cluster=~".*-(ci|qa)", namespace!="dagster"} > 0)'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="KubernetesJobFailedCritical",
                condition="B",
                for_="5m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "Job {{ $labels.job_name }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has failed."
                },
                datas=rd(
                    'sum by (cluster, namespace, job_name) (kube_job_status_failed{cluster=~".*-(production)", namespace!="dagster"} > 0)'
                ),
            ),
            # --- HPA at max replicas ---
            # Fires when an HPA has been at its maximum replica count for 15m,
            # meaning the workload cannot scale further under load.
            alerting.RuleGroupRuleArgs(
                name="HPAAtMaxReplicasWarning",
                condition="B",
                for_="15m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "HPA {{ $labels.horizontalpodautoscaler }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has been at its maximum replica count for 15 minutes. The workload may be unable to scale further under load."
                },
                datas=rd(
                    'sum by (cluster, namespace, horizontalpodautoscaler) (kube_horizontalpodautoscaler_status_current_replicas{cluster=~".*-(ci|qa)"}) >= sum by (cluster, namespace, horizontalpodautoscaler) (kube_horizontalpodautoscaler_spec_max_replicas{cluster=~".*-(ci|qa)"})'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="HPAAtMaxReplicasCritical",
                condition="B",
                for_="15m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "HPA {{ $labels.horizontalpodautoscaler }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has been at its maximum replica count for 15 minutes. The workload may be unable to scale further under load."
                },
                datas=rd(
                    'sum by (cluster, namespace, horizontalpodautoscaler) (kube_horizontalpodautoscaler_status_current_replicas{cluster=~".*-(production)"}) >= sum by (cluster, namespace, horizontalpodautoscaler) (kube_horizontalpodautoscaler_spec_max_replicas{cluster=~".*-(production)"})'
                ),
            ),
        ],
        opts=resource_opts,
    )

    # -------------------------------------------------------------------------
    # Linux host rules
    # Source: grafana-alerts/cortex-rules/linux-host.yaml
    #
    # These rules have no cluster label filter — they apply to all EC2 hosts
    # regardless of environment and fire on whichever stack scrapes them.
    # Three separate groups to preserve the original evaluation intervals.
    # -------------------------------------------------------------------------

    # CPU usage > 80% sustained for 6 hours (warning only — no critical rule).
    alerting.RuleGroup(
        "linux-host-cpu-usage",
        name="cpu-usage",
        folder_uid=alerts_folder.uid,
        interval_seconds=3600,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="CPUUsageWarning",
                condition="B",
                for_="6h",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": 'CPU usage on {{ $labels.instance }} has been at {{ printf "%.2f" $value }} for at least 6 hours.'
                },
                datas=rd(
                    "1 - (\n"
                    '  sum by (cluster, instance) (rate(host_cpu_seconds_total{mode="idle", job="integrations/linux_host"}[5m]))\n'
                    '  / sum by (cluster, instance) (rate(host_cpu_seconds_total{job="integrations/linux_host"}[5m]))\n'
                    ") > 0.8"
                ),
            ),
        ],
        opts=resource_opts,
    )

    # Memory usage > 90% sustained for 2 hours (warning only).
    alerting.RuleGroup(
        "linux-host-memory-usage",
        name="memory-usage",
        folder_uid=alerts_folder.uid,
        interval_seconds=1800,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="MemoryUsageWarning",
                condition="B",
                for_="2h",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": 'Memory usage on {{ $labels.instance }} has been at {{ printf "%.2f" $value }} for at least 2 hours.'
                },
                datas=rd("(host_memory_used_bytes / host_memory_total_bytes) > 0.9"),
            ),
        ],
        opts=resource_opts,
    )

    # Disk usage thresholds: warning > 80% for 1h, critical > 95% for 10m.
    # Excludes pseudo-filesystems (squashfs, vfat) and non-/dev/ devices.
    alerting.RuleGroup(
        "linux-host-disk-usage",
        name="disk-usage",
        folder_uid=alerts_folder.uid,
        interval_seconds=600,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="DiskUsageWarning",
                condition="B",
                for_="1h",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": 'Filesystem on {{ $labels.device }} at {{ $labels.instance }} is {{ printf "%.2f" $value }}% full.'
                },
                datas=rd(
                    '(host_filesystem_used_ratio{device=~"/dev.*",filesystem!~"(squashfs|vfat)",job="integrations/linux_host"} * 100) > 80'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="DiskUsageCritical",
                condition="B",
                for_="10m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": 'Filesystem on {{ $labels.device }} at {{ $labels.instance }} is {{ printf "%.2f" $value }}% full.'
                },
                datas=rd(
                    '(host_filesystem_used_ratio{device=~"/dev.*",filesystem!~"(squashfs|vfat)",job="integrations/linux_host"} * 100) > 95'
                ),
            ),
        ],
        opts=resource_opts,
    )
