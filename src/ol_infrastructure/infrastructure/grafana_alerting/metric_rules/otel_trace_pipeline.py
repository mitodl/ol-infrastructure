"""Alerting on the trace pipeline itself: silent services and the tail sampler.

A service with a broken exporter and a quiet service look the same in Tempo.
ol-analytics-api emitted no spans for its whole lifetime and nobody noticed,
and the tail sampler's `keep-low-volume-services` keep-list (see
substructure/aws/eks/grafana.py) is only as good as someone remembering to
add to it. These rules turn "nobody is looking" into a notification.

Silent service: absent after present, not "deployed but absent"
----------------------------------------------------------------
The obvious rule joins what is deployed against what reaches Tempo. There is
nothing to join against yet. kube-state-metrics only emits
`kube_<resource>_labels` for resources named in its `metricLabelsAllowlist`
(substructure/aws/eks/grafana.py), which today covers jobs and nodes, so
`kube_deployment_labels` does not exist in any stack, and OTEL_SERVICE_NAME is
a container env var no metric exposes. Even with ol.mit.edu/application on
every Deployment, it maps to OTel service names one-to-many (edxapp alone is
lms, cms, their celery workers and beat), so the join would need its own
maintained mapping.

So this fires on a regression instead: a service with SERVER spans in the 24h
before the last 24h and none since. It cannot catch a service that
has never emitted (the ol-analytics-api case). It does catch an exporter,
collector, or instrumentation change that silences a service that used to
work, and a service leaving the keep-list and sampling into invisibility.

Why SERVER spans, and why 24h
------------------------------
Measured 2026-09-28 over 6 days of hourly evaluations (144) of
traces_spanmetrics_calls_total:

  - Without a span_kind filter, the same expression would have fired for
    four celery workers (mitx-staging cms/lms-celery, both lms-high-mem-celery
    queues), for up to 96 of the 144 evaluations. Those workers are
    event-driven and go days without a task.
  - Restricted to SPAN_KIND_SERVER it fired zero times in production and QA.
    The quietest request-serving service over the 7 days to 2026-09-28 had a
    24h minimum of about 2 server spans (the ToolHive vMCP tier, in both
    stacks), so one idle day there will fire it; every other one was in the
    hundreds or more.
  - At 12h, production-witan's SERVER spans reached zero, so the window
    can't be shorter without per-service thresholds.

A deliberately removed service starts firing 24h after its last span and
resolves 24h later, when that span leaves the baseline window. That is the
cost of not maintaining an allowlist.

Aggregated by service, not cluster: a service silent in one cluster but
still emitting in another is not caught.

The sampler rules
-----------------
`sampling_traces_on_memory` is the tail sampler's buffer. Once it reaches
`numTraces` (50,000 per pod in substructure/aws/eks/grafana.py), the
processor evicts traces before their decision, which is how 98% of traces
were lost before the 2026-08 sizing fix. Peak over the 7 days to 2026-09-28
was 7,284 on data-production. The warning sits at 25,000, half of
`numTraces`, so there is room to resize before loss starts. Keep it in step
if `numTraces` changes.

`sampling_trace_dropped_too_early_total` counts that loss directly. Its series
was present and flat at 0 on every production cluster over the same 7 days, so
any increase is new. The series is per sampler pod (`instance` is the pod IP),
so a replaced pod starts a new series. `increase()` needs two samples and
cannot see drops that land before the new pod's first scrape, so the rule has
a second arm (`x unless x offset 15m`) for a series whose first value is
already non-zero, the same workaround clickhouse.py uses. Its `> 0` keeps a
new pod that starts at 0 quiet; over the 7 days to 2026-09-28 three clusters
replaced a sampler pod and the full expression fired zero times.

Routing
-------
All three carry `channel=devops-warnings`: none of them is an outage, and
each wants a person to look within the day, not a page. `service_name` is
copied from spanmetrics' `service` label because `service_name`, not
`service`, is in alertmanager.py's `group_bies`, so each silent service gets
its own notification.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

_CALLS = 'traces_spanmetrics_calls_total{span_kind="SPAN_KIND_SERVER"}'

# Half of the sampler's per-pod numTraces in substructure/aws/eks/grafana.py.
_SAMPLER_BUFFER_WARN_TRACES = 25000

_DROPPED = "otelcol_processor_tail_sampling_sampling_trace_dropped_too_early_total"

_ROUTING = {"channel": "devops-warnings"}

_SILENT_SERVICE_EXPR = (
    "label_replace("
    f"(sum by (service) (increase({_CALLS}[1d] offset 1d)) > 0)"
    " unless on (service) "
    f"(sum by (service) (increase({_CALLS}[1d])) > 0)"
    ', "service_name", "$1", "service", "(.+)")'
)


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create alert rules for silent services and tail-sampler health."""
    alerting.RuleGroup(
        "otel-trace-pipeline",
        name="otel-trace-pipeline",
        folder_uid=folder_uid,
        interval_seconds=900,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="TempoServiceSilent",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", **_ROUTING},
                annotations={
                    "summary": "{{ $labels.service_name }} has sent no server spans to Tempo for 24h",
                    "description": "{{ $labels.service_name }} produced SERVER spans in the 24h before the last 24h and none since. Either it stopped serving requests or its trace export broke (exporter config, collector, instrumentation). A service that was removed on purpose fires for a day and then resolves.",
                },
                datas=rd(_SILENT_SERVICE_EXPR),
            ),
        ],
        opts=resource_opts,
    )

    alerting.RuleGroup(
        "otel-tail-sampler",
        name="otel-tail-sampler",
        folder_uid=folder_uid,
        interval_seconds=60,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="TailSamplerBufferHigh",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", **_ROUTING},
                annotations={
                    "summary": "Tail sampler {{ $labels.pod }} on {{ $labels.cluster }} is holding over half its trace buffer",
                    "description": f"{{{{ $labels.pod }}}} on {{{{ $labels.cluster }}}} has held more than {_SAMPLER_BUFFER_WARN_TRACES} in-flight traces for 15 minutes. At numTraces (50,000 per pod) the sampler evicts traces before deciding on them. Raise numTraces and the sampler's memory limit in substructure/aws/eks/grafana.py, or add sampler replicas.",
                },
                datas=rd(
                    "max by (cluster, pod) "
                    "(otelcol_processor_tail_sampling_sampling_traces_on_memory)"
                    f" > {_SAMPLER_BUFFER_WARN_TRACES}"
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="TailSamplerDroppingTraces",
                condition="C",
                for_="0m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", **_ROUTING},
                annotations={
                    "summary": "Tail sampler on {{ $labels.cluster }} is dropping traces before deciding on them",
                    "description": "otelcol_processor_tail_sampling_sampling_trace_dropped_too_early_total increased on {{ $labels.cluster }} in the last 15 minutes. Those traces never reach Tempo, and every span-derived metric computed from them is skewed. The buffer is too small for the current trace rate: raise numTraces in substructure/aws/eks/grafana.py.",
                },
                datas=rd(
                    f"sum by (cluster) (increase({_DROPPED}[15m])) > 0\n"
                    "or\n"
                    f"sum by (cluster) ({_DROPPED} unless {_DROPPED} offset 15m) > 0"
                ),
            ),
        ],
        opts=resource_opts,
    )
