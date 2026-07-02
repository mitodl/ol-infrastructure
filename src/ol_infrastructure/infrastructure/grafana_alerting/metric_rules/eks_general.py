"""EKS workload alert rules.

Source: grafana-alerts/cortex-rules/eks_general.yaml

Warning rules filter cluster=~".*-(ci|qa)"    — fire on CI and QA stacks.
Critical rules filter cluster=~".*-(production)" — fire on prod stack only.
Rules with no matching data on a given stack stay silent (no_data_state=OK).
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create EKS workload alert rule groups."""
    alerting.RuleGroup(
        "eks-general",
        name="general",
        folder_uid=folder_uid,
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
