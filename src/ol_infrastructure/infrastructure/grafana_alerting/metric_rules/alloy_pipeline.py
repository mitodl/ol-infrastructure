"""Alerting on an Alloy config that a running collector rejected.

What happened 2026-10-23 to 2026-10-26
--------------------------------------
#6538 added a stage.match selector with a backtick-quoted line filter to
podLogsViaLoki.extraLogProcessingStages (substructure/aws/eks/grafana.py).
Alloy v1.20.0's selector parser rejects backticks, so from about 14:30Z on
10-23 every alloy-logs pod in every production cluster failed to load the
new pod_logs pipeline. The failure split two ways:

  - Pods started after the rollout (every new or Karpenter-replaced node)
    failed their initial load and crash-looped, hundreds of restarts each.
    No log from any pod on those nodes reached Loki: all of open-metadata
    and part of dagster on data-production, for three days.
    PodCrashLoopingCritical (eks_general.py) fired for them from 15:30Z.
  - Pods that existed before the rollout kept their last good config, kept
    shipping and never restarted. They logged "failed to reload config" and
    reported loki.process.pod_logs and pod_logs_via_loki.feature unhealthy,
    which nothing alerted on.

#6606 fixed the selector and the errors stopped about 14:50Z on 10-26.

Why a rule for the second half
------------------------------
On a cluster whose nodes do not churn, a rejected reload produces no crash
loop at all: every pod keeps the old config, and the change just never takes
effect. It is also why the drops from #6517 and #6528 never applied on the
pods that kept running (#6606). `alloy_component_controller_running_components`
with health_type="unhealthy" is the one signal those pods give, and it names
the cause where a crash-looping pod only names the symptom.

Over 09-26 to 10-23 13:30Z no Alloy instance held an unhealthy component for
30m. During the incident it held at 2 per pre-existing alloy-logs pod on
applications-, data-, operations- and residential-production.

Scope
-----
Each k8s Alloy pod's metrics reach Mimir twice: once from the cluster scrape,
labeled with cluster, namespace and pod, and once from its own self-report,
labeled with collector_id and an instance named after the pod but no cluster.
The rule reads only the cluster scrape (collector_id=""), so each pod alerts
once and names its cluster. Alloy on EC2 is not covered: its self-report
(src/bilder/components/alloy/files/config.alloy) keeps only alloy_build_* and
a few prometheus_* series, so this metric never leaves the instance.

Routing
-------
`channel=devops-warnings`: a collector on a stale config is not an outage,
but it wants a person within the day, because the next pod that starts on
that config collects nothing.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

_ROUTING = {"channel": "devops-warnings"}

_UNHEALTHY_EXPR = (
    "sum by (cluster, namespace, pod) "
    "(alloy_component_controller_running_components"
    '{health_type="unhealthy", collector_id=""}) > 0'
)


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create the alert rule for Alloy instances with unhealthy components."""
    alerting.RuleGroup(
        "alloy-pipeline",
        name="alloy-pipeline",
        folder_uid=folder_uid,
        interval_seconds=300,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="AlloyComponentUnhealthy",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", **_ROUTING},
                annotations={
                    "summary": "Alloy {{ $labels.pod }} on {{ $labels.cluster }} has unhealthy components",
                    "description": 'Alloy {{ $labels.namespace }}/{{ $labels.pod }} on {{ $labels.cluster }} has reported unhealthy components for 30 minutes. Usually a config reload was rejected: this pod keeps running its last good config, but any Alloy pod that starts now fails its initial load and collects nothing. Look for "failed to reload config" in the pod\'s logs; for the k8s-monitoring collectors the config comes from substructure/aws/eks/grafana.py.',
                },
                datas=rd(_UNHEALTHY_EXPR),
            ),
        ],
        opts=resource_opts,
    )
