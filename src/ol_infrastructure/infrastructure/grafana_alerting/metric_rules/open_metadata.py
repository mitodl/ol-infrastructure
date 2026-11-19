"""Alerting on OpenMetadata ingestion that has stopped starting.

What happened 2026-10-30 to 2026-11-05
--------------------------------------
The omjob-operator ran out of JVM heap (``OutOfMemoryError: Java heap space``)
on data-production at 2026-10-30T15:43Z and again at 2026-11-04T22:10Z, and on
data-qa at 2026-11-03T00:33Z. The error killed the operator's watch, worker and
timer threads one by one but never the process, so the container stayed
Running with zero restarts and no restart or OOMKilled rule fired. No CronOMJob
ran until the pod was replaced (the first production stall lasted until
2026-11-02T21:53Z). The heap went to the operator's cache of ~1,800 OMJobs,
which nothing deleted (applications/open_metadata/__main__.py,
omjob_cleanup_cron_job).

Why a stall rule and not the OOM log line
-----------------------------------------
An operator that stops reconciling for any other reason looks the same from
outside: CronOMJobs stop producing pods. ``kube_pod_start_time`` of the OMJob
main pods (``om-ingest-<cronomjob>-<epoch>-main``) catches that whatever the
cause. data-production had one such stall from about 07:00 to 16:00Z on
2026-11-03 with no OutOfMemoryError logged. The ``max_over_time`` over 7d keeps
the last start visible after the pod itself has been deleted. A stall that
outlasts the window leaves no series, and NoData maps to OK so that stacks
without OpenMetadata stay quiet, so the alert resolves after 7 days of stall
while ingestion is still down.

The 3h threshold leans on ``om-ingest-team-keycloak-sync``, which runs hourly
in every environment (substructure/open_metadata/__main__.py). Every other
CronOMJob is daily or weekly, and between 09:00 and 02:00 UTC they leave a 17h
gap, so the threshold has to grow to a day if the hourly job goes away. Over
2026-10-22 to 2026-10-29, after the hourly job started and before the first
OOM, the gap never exceeded 3h on data-production.

Routing
-------
``channel=devops-warnings``: stale catalog metadata is not an outage, but it
wants a person within the day.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

_ROUTING = {"channel": "devops-warnings"}

_STALL_THRESHOLD_SECONDS = 3 * 60 * 60

_STALL_EXPR = (
    "time() - max by (cluster) (max_over_time(kube_pod_start_time"
    '{namespace="open-metadata", pod=~"om-ingest-.+-main"}[7d])) '
    f"> {_STALL_THRESHOLD_SECONDS}"
)


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create the alert rule for OpenMetadata ingestion pods that stopped starting."""
    alerting.RuleGroup(
        "open-metadata",
        name="open-metadata",
        folder_uid=folder_uid,
        interval_seconds=300,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="OpenMetadataIngestionStalled",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", **_ROUTING},
                annotations={
                    "summary": "No OpenMetadata ingestion pod has started on {{ $labels.cluster }} in 3 hours",
                    "description": 'No OMJob main pod has started in open-metadata on {{ $labels.cluster }} for over 3 hours, although om-ingest-team-keycloak-sync runs hourly. The omjob-operator has most likely stopped reconciling: check `kubectl -n open-metadata logs deploy/openmetadata-omjob-operator` for "OutOfMemoryError" or "listSyncAndWatch failed", and `kubectl -n open-metadata get omjobs` for a pile-up the omjob-cleanup CronJob should have cleared. Restarting the Deployment resumes scheduling.',
                },
                datas=rd(_STALL_EXPR),
            ),
        ],
        opts=resource_opts,
    )
