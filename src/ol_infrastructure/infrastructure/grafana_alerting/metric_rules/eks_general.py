"""EKS workload alert rules.

Source: grafana-alerts/cortex-rules/eks_general.yaml

Warning rules filter cluster=~".*-(ci|qa)"    — fire on CI and QA stacks.
Critical rules filter cluster=~".*-(production)" — fire on prod stack only.
Rules with no matching data on a given stack stay silent (no_data_state=OK).

exec_err_state (query-evaluation failure, distinct from no_data_state) was
never set here before 2026-08, so it silently defaulted to Grafana's
"Alerting" -- meaning a transient datasource blip (confirmed: a 2026-07-09
provisioning race on the CI stack) escalated into a real, severity-labelled,
Rootly-routed page for every single rule that failed to evaluate. Warning
rules get exec_err_state="OK" -- same reasoning as no_data_state=OK, since a
warning-tier rule going silent during an error is an acceptable trade.
Critical rules get exec_err_state="KeepLast" instead of "OK": going silent on
a production-critical rule during an error is not acceptable (it could mask
a genuine ongoing incident), but flipping to Alerting on every transient blip
isn't either, so the rule holds its last known state through the error.
Neither choice makes a datasource-wide outage visible on its own; that needs
a dedicated canary rule, tracked separately (not yet added -- see
tk-exec-err-state-is-never-set-every-grafana-rule-p-3aa620).
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
                condition="C",
                for_="10m",
                no_data_state="OK",
                exec_err_state="OK",
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
                condition="C",
                for_="10m",
                no_data_state="OK",
                exec_err_state="KeepLast",
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
                condition="C",
                for_="10m",
                no_data_state="OK",
                exec_err_state="OK",
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
                condition="C",
                for_="10m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical"},
                annotations={
                    "description": "There is a mismatch between the requested number of instances for deployment {{ $labels.deployment }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }}."
                },
                datas=rd(
                    'sum by (cluster, namespace, deployment) (kube_deployment_status_replicas_available{cluster=~".*-(production)"}) / sum by (cluster, namespace, deployment) (kube_deployment_status_replicas) < 1.0'
                ),
            ),
            # --- Deployment availability ---
            # Fires only when a deployment has zero available replicas -- a genuine
            # total outage, matching what "is not available" actually implies.
            #
            # This used to key off kube_deployment_status_condition's "Available"
            # condition instead, which is NOT the same thing: that condition goes
            # false once availableReplicas drops below desiredReplicas -
            # maxUnavailable, not below 1. maxUnavailable is commonly expressed as a
            # percentage (e.g. "25%"), and Kubernetes rounds percentage-based
            # maxUnavailable DOWN, so on a low-replica-count deployment (3 replicas *
            # 25% = 0.75, rounded down to 0) the condition flips false the moment a
            # single pod is lost -- e.g. during a slow image pull while a node is
            # being replaced. Confirmed directly on xpro-production-edxapp-lms-webapp
            # on 2026-07-27: replicas_available only ever dropped 3 -> 2, never to 0,
            # yet this alert fired as if the whole deployment were down. Partial
            # degradation like that is already covered by DeploymentReplicasMissing
            # above at the appropriate (lower) severity; this rule should only catch
            # the case its name promises.
            #
            # The "and ... spec_replicas > 0" guard excludes deployments deliberately
            # scaled to zero (e.g. a disabled service in a staging namespace) -- those
            # sit at 0/0 available/desired indefinitely, which is not an outage.
            #
            # The nonzero-valued clause (spec_replicas > 0) is deliberately written
            # FIRST. Every rule in this pipeline feeds into a fixed threshold stage
            # that fires on last(A) > 0 (see base.py's _rule_data), and PromQL's
            # `and` binary operator carries over the *value* of its left-hand side,
            # not a boolean 1/0 -- so "available == 0 and spec_replicas > 0" would
            # return the left side's value, which is 0 by construction, and 0 > 0
            # never fires. Putting the nonzero clause on the left instead means the
            # surviving series carries spec_replicas' own (always-positive, since
            # it's already filtered > 0) value through to the threshold stage.
            alerting.RuleGroupRuleArgs(
                name="DeploymentUnavailableWarning",
                condition="C",
                for_="10m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "A deployment {{ $labels.deployment }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} is not available for an extended period of time."
                },
                datas=rd(
                    'sum by (cluster, namespace, deployment) (kube_deployment_spec_replicas{cluster=~".*-(ci|qa)"}) > 0'
                    " and "
                    'sum by (cluster, namespace, deployment) (kube_deployment_status_replicas_available{cluster=~".*-(ci|qa)"}) == 0'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="DeploymentUnavailableCritical",
                condition="C",
                for_="10m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical"},
                annotations={
                    "description": "A deployment {{ $labels.deployment }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} is not available for an extended period of time."
                },
                datas=rd(
                    'sum by (cluster, namespace, deployment) (kube_deployment_spec_replicas{cluster=~".*-(production)"}) > 0'
                    " and "
                    'sum by (cluster, namespace, deployment) (kube_deployment_status_replicas_available{cluster=~".*-(production)"}) == 0'
                ),
            ),
            # --- StatefulSet replicas ---
            # Fires when ready replicas / desired replicas < 1.0.
            #
            # for_ is 20m, not the more typical 10m, because EBS-backed StatefulSets
            # (Typesense, etc.) can legitimately drop below full readiness for well
            # over 10 minutes with no real problem: when Karpenter replaces an
            # underlying node, more than one pod can be evicted at once, and each one
            # waits on its EBS volume to detach from the old instance and reattach to
            # the new one before it can even start. Observed directly on
            # mitxonline-ts-sts on 2026-07-27: 2 of 3 replicas went down together and
            # readiness didn't fully recover for ~17 minutes, entirely via normal
            # volume reattachment with no crash-looping or intervention involved --
            # comfortably past the old 10m threshold. A StatefulSet genuinely stuck
            # (e.g. a corrupted Raft log requiring a manual data wipe, as seen
            # separately on mitx-staging-ts-sts) stays down far longer than 20m
            # either way, so this doesn't meaningfully delay catching a real issue.
            alerting.RuleGroupRuleArgs(
                name="StatefulSetReplicasMissingWarning",
                condition="C",
                for_="20m",
                no_data_state="OK",
                exec_err_state="OK",
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
                condition="C",
                for_="20m",
                no_data_state="OK",
                exec_err_state="KeepLast",
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
                condition="C",
                for_="5m",
                no_data_state="OK",
                exec_err_state="OK",
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
                condition="C",
                for_="5m",
                no_data_state="OK",
                exec_err_state="KeepLast",
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
            #
            # keep_firing_for + missing_series_evals_to_resolve attack the
            # storm mechanism directly: a churning pod's series vanishes the
            # moment Kubernetes replaces it (new pod name), which reads as
            # the alert resolving -- and then the replacement pod mints a
            # brand new alert instance under its own name the moment it
            # starts crash-looping too. keep_firing_for holds the alert open
            # across that gap; missing_series_evals_to_resolve stops a
            # vanished series from resolving-and-refiring in the first
            # place. See docs/plans/grafana-alerting-remediation-spec.md §3a.
            alerting.RuleGroupRuleArgs(
                name="PodCrashLoopingWarning",
                condition="C",
                for_="5m",
                no_data_state="OK",
                exec_err_state="OK",
                keep_firing_for="30m",
                missing_series_evals_to_resolve=10,
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
                condition="C",
                for_="5m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                keep_firing_for="30m",
                missing_series_evals_to_resolve=10,
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
                condition="C",
                for_="5m",
                no_data_state="OK",
                exec_err_state="OK",
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
                condition="C",
                for_="5m",
                no_data_state="OK",
                exec_err_state="KeepLast",
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
            #
            # Warning ("> 0", ci/qa clusters only -- see cluster=~ below) is
            # for visibility/trend-tracking in lower environments; there is
            # no equivalent lower-severity signal for production, only
            # Critical below. Several production workloads (e.g. apisix,
            # mitlearn-app, mitxonline-app) intentionally launch pods at a
            # low memory floor and rely on a VPA to raise limits toward a
            # ceiling based on observed usage, so a single OOM-and-recover
            # right after a deploy is expected, self-healing behavior, not an
            # incident -- Critical's threshold below is set high enough that
            # this case produces no alert in production at all, paging or
            # otherwise.
            #
            # Critical requires ">= 3", i.e. at least 3 real restarts within
            # the window, so a lone self-healing OOM doesn't page anyone,
            # while a container that's genuinely stuck crash-looping still
            # trips it within a few minutes (container restart backoff is
            # short). NOTE: increase() extrapolates over the range, so N real
            # restarts commonly report as a value just above N rather than
            # exactly N -- observed directly in production: single-restart
            # pods reported 1.008-1.017, and a double-restart pod reported
            # ~2.034. A raw ">2" threshold would therefore fire on 2 real
            # restarts, not 3 as the description below states. We use ">= 3"
            # rather than "> 3": extrapolation only pushes the value *up*
            # from the raw integer delta, and only reaches exactly the raw
            # integer (e.g. exactly 3.0 for 3 real restarts) in the edge case
            # where a sample lands exactly on each range boundary, which can
            # happen when scrape and rule-evaluation timestamps align. A
            # strict "> 3" would then wrongly require a 4th restart in that
            # case; ">= 3" reliably means "at least 3 real restarts" either
            # way, since extrapolation can't push 2 real restarts' value
            # anywhere near 3.
            # keep_firing_for + missing_series_evals_to_resolve: same
            # replaced-pod storm mechanism as PodCrashLooping above.
            alerting.RuleGroupRuleArgs(
                name="PodOOMKilledWarning",
                condition="C",
                for_="5m",
                no_data_state="OK",
                exec_err_state="OK",
                keep_firing_for="30m",
                missing_series_evals_to_resolve=10,
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
                condition="C",
                for_="5m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                keep_firing_for="30m",
                missing_series_evals_to_resolve=10,
                labels={"severity": "critical"},
                annotations={
                    "description": "Container {{ $labels.container }} in pod {{ $labels.pod }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has been OOMKilled and is repeatedly restarting (3+ restarts within the past hour). Memory limits may need to be increased."
                },
                datas=rd(
                    "sum by (cluster, namespace, pod, container) (\n"
                    '  (kube_pod_container_status_last_terminated_reason{cluster=~".*-(production)", reason="OOMKilled"} == 1)\n'
                    "  * on (cluster, namespace, pod, container) group_left()\n"
                    "  (increase(kube_pod_container_status_restarts_total[1h]) >= 3)\n"
                    ")"
                ),
            ),
            # --- Job failures ---
            # Fires when a Job reaches the Failed condition -- i.e. it exhausted its
            # backoffLimit and gave up.
            #
            # Deliberately NOT kube_job_status_failed > 0, which counts failed PODS
            # rather than failed JOBS. A Job that burns a retry and then succeeds keeps
            # a non-zero failed-pod count for the rest of its life, so that expression
            # alerts on work that completed fine. Observed the day this rule first
            # became deliverable: mitlearn-app-pre-deploy in applications-qa sat at
            # `Complete, succeeded=1, failed=2` and paged anyway, and for_="5m" did not
            # save it because the Job ran for 7m1s. Over 7 days in production the same
            # shape covered 11 distinct xqueue-grader-* jobs in
            # mitxonline-openedx-graders; kube_job_failed{condition="true"} has no
            # series at all for any of them.
            #
            # This was pre-existing logic rather than a regression -- it simply never
            # mattered while every firing went to oblivion. Making the rule deliverable
            # is what exposed it.
            #
            # Named Workload* rather than Kubernetes*: alertmanager.py silences
            # `alertname =~ "Kube.*"` (built-in k8s noise) with continue_=False, and
            # that route sits ABOVE the severity routes. Under the old
            # KubernetesJobFailed* names these rules matched it, so despite carrying
            # severity labels they were delivered to oblivion -- 254 firings in 30
            # days on the production stack and 104 on QA, none of which reached
            # Rootly. Keep any new rule name here clear of the Kube.* prefix.
            #
            # Exclusions:
            #   dagster          -- manages its own job retry logic.
            #   witan-ci-indexer -- genuinely reaches the Failed condition on both
            #                       operations-qa and operations-production (confirmed
            #                       under kube_job_failed{condition="true"}, not just
            #                       the noisier failed-pod signal above); excluded so
            #                       the rename does not immediately page for a known
            #                       break. Remove this exclusion once that job is
            #                       fixed -- see tk-witan-ci-indexer-cronjob-fails-on
            #                       -nearly-every-r-4c1462.
            alerting.RuleGroupRuleArgs(
                name="WorkloadJobFailedWarning",
                condition="C",
                for_="5m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "Job {{ $labels.job_name }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has failed."
                },
                datas=rd(
                    'sum by (cluster, namespace, job_name) (kube_job_failed{cluster=~".*-(ci|qa)", condition="true", namespace!="dagster", job_name!~"witan-ci-indexer.*"} == 1)'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="WorkloadJobFailedCritical",
                condition="C",
                for_="5m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical"},
                annotations={
                    "description": "Job {{ $labels.job_name }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has failed."
                },
                datas=rd(
                    'sum by (cluster, namespace, job_name) (kube_job_failed{cluster=~".*-(production)", condition="true", namespace!="dagster", job_name!~"witan-ci-indexer.*"} == 1)'
                ),
            ),
            # --- HPA at max replicas ---
            # Fires when an HPA has been at its maximum replica count for 15m,
            # meaning the workload cannot scale further under load.
            #
            # Excludes HPAs where min_replicas == max_replicas (e.g. a
            # single-fixed-replica HPA with min=max=1): those are permanently
            # "at max" by construction, so the condition is always true and
            # carries no signal about the workload actually being saturated.
            # Observed in production 2026-07: xqwatcher's HPAs (min=max=1)
            # fired this rule continuously, unrelated to any real incident.
            #
            # At-max is a capacity fact, not an incident -- one resize
            # decision, not a page. `channel: devops-warnings` routes it to a
            # dedicated Slack channel and terminates there (alertmanager.py's
            # top policy branch, continue_=False), so it never reaches Rootly
            # at all -- deliberately not a `severity` tier change, since that
            # would route it through Rootly's urgency/escalation machinery
            # instead of bypassing it. See
            # docs/plans/grafana-alerting-remediation-spec.md §3c.
            #
            alerting.RuleGroupRuleArgs(
                name="HPAAtMaxReplicasWarning",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", "channel": "devops-warnings"},
                annotations={
                    "description": "HPA {{ $labels.horizontalpodautoscaler }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has been at its maximum replica count for 15 minutes. The workload may be unable to scale further under load."
                },
                datas=rd(
                    'sum by (cluster, namespace, horizontalpodautoscaler) (kube_horizontalpodautoscaler_status_current_replicas{cluster=~".*-(ci|qa)"}) >= sum by (cluster, namespace, horizontalpodautoscaler) (kube_horizontalpodautoscaler_spec_max_replicas{cluster=~".*-(ci|qa)"}) and sum by (cluster, namespace, horizontalpodautoscaler) (kube_horizontalpodautoscaler_spec_max_replicas{cluster=~".*-(ci|qa)"}) != sum by (cluster, namespace, horizontalpodautoscaler) (kube_horizontalpodautoscaler_spec_min_replicas{cluster=~".*-(ci|qa)"})'
                ),
            ),
            # Also excludes the mitxonline hubspot-sync celery worker HPA:
            # a scheduled certificate-generation task enqueues ~20k contact
            # sync tasks every 6 hours, and the worker legitimately sits at
            # max replicas for ~20 minutes while draining, throughput-capped
            # by the HubSpot API rate limiter rather than replica count.
            # Remove the exclusion once the producer stops enqueueing no-op
            # syncs: https://github.com/mitodl/hq/issues/12701
            alerting.RuleGroupRuleArgs(
                name="HPAAtMaxReplicasCritical",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical", "channel": "devops-warnings"},
                annotations={
                    "description": "HPA {{ $labels.horizontalpodautoscaler }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has been at its maximum replica count for 15 minutes. The workload may be unable to scale further under load."
                },
                datas=rd(
                    'sum by (cluster, namespace, horizontalpodautoscaler) (kube_horizontalpodautoscaler_status_current_replicas{cluster=~".*-(production)", horizontalpodautoscaler!="keda-hpa-mitxonline-hubspot-sync-celery-worker"}) >= sum by (cluster, namespace, horizontalpodautoscaler) (kube_horizontalpodautoscaler_spec_max_replicas{cluster=~".*-(production)"}) and sum by (cluster, namespace, horizontalpodautoscaler) (kube_horizontalpodautoscaler_spec_max_replicas{cluster=~".*-(production)"}) != sum by (cluster, namespace, horizontalpodautoscaler) (kube_horizontalpodautoscaler_spec_min_replicas{cluster=~".*-(production)"})'
                ),
            ),
            # --- CronJob staleness ---
            # Job-failure alerting above only sees runs that STARTED and failed. A
            # CronJob that stops firing while still existing -- suspended, controller
            # wedged, every run failing -- produces no successful run and no failed
            # Job to alert on. That is the gap that let an empty Open edX
            # course-search index sit unnoticed for months.
            #
            # What this does NOT cover is a CronJob that stops EXISTING: delete it and
            # kube-state-metrics drops the series, which is NoData, which
            # no_data_state=OK keeps silent. Detecting a resource that should be there
            # and isn't isn't an age-of-last-success question at all -- it needs an
            # expected-inventory check, which is a different rule (see the gap note
            # below, which the same `unless` construction would answer).
            #
            # Two buckets because PromQL cannot parse a cron expression to derive a
            # per-job threshold. Membership is an explicit cronjob list; validate a
            # new entry against its `schedule` label on kube_cronjob_info before
            # adding it. Current inventory:
            #   0/5 * * * *  cron-deploy-pipelines, cron-reindex   -> fast
            #   17 * * * *   witan-token-sync                      -> fast
            #   20 3 * * *   omnigraph-optimize                    -> slow
            #
            # omnigraph-optimize is NIGHTLY and the slow bucket is 15 days, so
            # compaction can stop for two weeks before this says anything.
            # metric_rules/witan.py carries a 36h rule for that CronJob
            # specifically, deliberately overlapping this one rather than
            # editing the membership here -- moving it out would change
            # alerting for cms-edxapp-reindex-courses too. If a third bucket is
            # ever added for daily jobs, fold that rule into it and delete it
            # there.
            #   20 4 * * 0   omnigraph-cleanup                     -> slow
            #   30 7 * * 0   cms-edxapp-reindex-courses            -> slow
            #
            # Two CronJobs are deliberately absent from both buckets:
            #
            #   witan-break-glass -- schedule `0 0 31 2 *`, February 31st, a date that
            #     never occurs, because it is triggered by hand. Permanently "stale"
            #     by design.
            #   witan-ci-indexer  -- same known break that is excluded from the
            #     job-failure rules above. It is not merely failing some runs: over a
            #     7-day window its age-since-last-success peaked at 518,714s (6.0
            #     days), so a 6h staleness rule would page for it continuously.
            #     Remove from both places together once it is fixed.
            #
            # `> 0` guards the never-yet-succeeded case: kube-state-metrics reports 0
            # (or omits the series) until a CronJob's first success, and time() minus
            # zero would otherwise fire instantly on every newly created CronJob.
            #
            # KNOWN GAP, deliberate: this cannot see a CronJob that has never
            # succeeded at all, because kube-state-metrics omits
            # last_successful_time entirely until the first success -- an absent
            # series is NoData, and no_data_state=OK keeps it silent. Two live
            # CronJobs are in exactly that state today (open-metadata's
            # cron-deploy-pipelines and cron-reindex in data-production have
            # neither a last_successful_time nor a last_schedule_time). Catching
            # that needs a separate `kube_cronjob_info unless
            # kube_cronjob_status_last_successful_time` rule, which is left out
            # here because it would page immediately for those two pre-existing
            # cases and for witan-break-glass. It also means a newly created
            # CronJob is uncovered until its first successful run.
            alerting.RuleGroupRuleArgs(
                name="ScheduledJobStaleFastWarning",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "CronJob {{ $labels.cronjob }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has not succeeded in over 6 hours."
                },
                datas=rd(
                    "max by (cluster, namespace, cronjob) (time() - "
                    "(kube_cronjob_status_last_successful_time"
                    '{cluster=~".*-(ci|qa)", cronjob=~"cron-deploy-pipelines|cron-reindex|witan-token-sync"} > 0)) > 21600'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="ScheduledJobStaleFastCritical",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical"},
                annotations={
                    "description": "CronJob {{ $labels.cronjob }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has not succeeded in over 6 hours."
                },
                datas=rd(
                    "max by (cluster, namespace, cronjob) (time() - "
                    "(kube_cronjob_status_last_successful_time"
                    '{cluster=~".*-(production)", cronjob=~"cron-deploy-pipelines|cron-reindex|witan-token-sync"} > 0)) > 21600'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="ScheduledJobStaleSlowWarning",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "CronJob {{ $labels.cronjob }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has not succeeded in over 15 days."
                },
                datas=rd(
                    "max by (cluster, namespace, cronjob) (time() - "
                    "(kube_cronjob_status_last_successful_time"
                    '{cluster=~".*-(ci|qa)", cronjob=~"omnigraph-optimize|omnigraph-cleanup|cms-edxapp-reindex-courses"} > 0)) > 1296000'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="ScheduledJobStaleSlowCritical",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical"},
                annotations={
                    "description": "CronJob {{ $labels.cronjob }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has not succeeded in over 15 days."
                },
                datas=rd(
                    "max by (cluster, namespace, cronjob) (time() - "
                    "(kube_cronjob_status_last_successful_time"
                    '{cluster=~".*-(production)", cronjob=~"omnigraph-optimize|omnigraph-cleanup|cms-edxapp-reindex-courses"} > 0)) > 1296000'
                ),
            ),
        ],
        opts=resource_opts,
    )
