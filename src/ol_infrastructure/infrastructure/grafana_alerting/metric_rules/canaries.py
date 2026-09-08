"""Alert rules for the Playwright canaries in src/ol_concourse/pipelines/canaries.

Written 2026-09. Those canaries drive a real browser through a real user journey
against a live property -- log in through Keycloak, search for a course -- on a
Concourse schedule. Until now they ran and nothing read the result: a red canary
was visible only to somebody looking at the Concourse UI.

What is already covered elsewhere, and deliberately not repeated here
----------------------------------------------------------------------
``synthetic_monitoring.py`` already probes MIT Learn's Next.js origin, its API
health endpoint and its homepage, and alerts on ``probe_success`` and request
latency. That answers "is the endpoint responding?". These rules answer "can a
person still log in and find a course?", which no HTTP probe can see -- a login
broken by an expired IdP client secret returns 200 on every URL those probes
touch. Neither is redundant with the other, and neither should be widened into
the other's job.

Why a pushed gauge and not Concourse's own build metrics
---------------------------------------------------------
Concourse's Prometheus emitter is enabled and vector ships it to this same Mimir,
including ``concourse_builds_latest_completed_build_status{teamName,pipelineName,
jobName}`` -- which looks exactly like the right signal and is not usable.

It is per-ATC-node in-memory state: each web node reports only the builds it
happened to observe finishing, and the nodes never reconcile. Measured on
2026-09-08, 8 job identities were reported by more than one node and 4 of those
disagreed *permanently* -- e.g. ``pulumi-kubewatch /
deploy-...-webhook-handler-applications-ci`` held 0 on one node and 3 on another
for the entire 3-hour window, both actively scraped. So ``max by (...)`` invents
failures that already recovered and ``min by (...)`` hides real ones, and web
nodes rotate, so the series churn on top. ``concourse_builds_failed_total``
carries no job labels at all (6 series, one per instance), so it cannot name a
canary either.

The canary therefore pushes its own verdict, which also means the signal survives
a Concourse upgrade changing its metric shape.

Why a gauge rather than a failure counter
------------------------------------------
``canary_journey_success`` is 1 or 0, pushed after every run. A counter would only
exist once incremented, so a canary that has never failed would have no series --
and ``increase()`` cannot tell that from a canary that stopped running, which is
the one failure a canary must never hide. With a gauge, both states are visible
and its *absence* is a third, separately alertable state. This is the same reason
``dashboards/base.py`` prefers counting from Loki over an Alloy-derived counter.

Why last_over_time() and not a bare selector
---------------------------------------------
The canary pushes once per run, every 10 minutes, and Prometheus marks a series
stale 5 minutes after its last sample. A bare ``canary_journey_success == 0``
therefore flickers in and out of existence between runs: the ``for_`` timer resets
on every gap and the rule spends half its life in NoData. ``last_over_time(...
[30m])`` holds the most recent verdict across the gap, so the series is continuous
and ``for_`` measures what it reads as measuring.

That also gives the ``for_`` durations their meaning. A verdict stays "last" until
the next run overwrites it, so at a 10-minute cadence a single failed run followed
by a pass is true for ~10 minutes and a 15-minute ``for_`` does not fire on it.
Two consecutive failures do. That is deliberately the same "a genuine outage fails
twice, a single blip does not page anyone" rule that ``playwright.config.ts`` sets
``retries: 1`` for.

Routing
-------
Both rules carry ``channel: devops-warnings`` and no ``severity``, so they reach
Slack and stay off the Rootly paging path. The only canary in the fleet targets
``rc.learn.mit.edu`` -- a release-candidate environment, where a broken journey is
something to look at on a weekday, not a 3am page. **Promoting a canary to the
paging path is a deliberate act**: add ``severity`` when a canary is pointed at a
production property, and read ``alertmanager.py``'s route tree first, because
``channel=devops-warnings`` is matched *above* the severity routes and carries no
``continue_``.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

# Canaries expected to be reporting. Absence has to be asserted against a name --
# `absent_over_time` returns no series to group by, so nothing can discover this
# list from the data. It is the one place a canary must be registered by hand:
# adding an entry to `canary_names` in src/ol_concourse/pipelines/canaries/meta.py
# deploys a canary, and adding it here is what notices when it stops reporting.
# A canary missing from this list is monitored only while it is running, which is
# the failure this rule exists to catch.
EXPECTED_CANARIES = [
    "mit-learn",
]

# One run every 10 minutes (CanaryParams.schedule_interval). The window has to
# span several runs so a single slow or requeued build is not read as an outage.
_ABSENCE_WINDOW = "30m"
# Bridges the gap between 10-minute pushes and Prometheus' 5-minute staleness.
_VERDICT_WINDOW = "30m"


def _journey_failing_expr() -> str:
    """Latest verdict of any canary is a failure."""
    return f"last_over_time(canary_journey_success[{_VERDICT_WINDOW}]) == 0"


def _not_reporting_expr(canary_names: list[str]) -> str:
    """Any canary that should be reporting has pushed nothing recently.

    One ``absent_over_time`` per name, OR-ed together, rather than one rule per
    canary: rule names have to be unique within a group, and ``absent_over_time``
    keeps the equality matchers from its selector as labels on the series it
    invents -- verified against Mimir -- so a single rule still produces a
    correctly labelled instance per missing canary and ``{{ $labels.canary }}``
    resolves in the annotation.
    """
    return " or ".join(
        "absent_over_time("
        f'canary_journey_success{{canary="{canary_name}"}}[{_ABSENCE_WINDOW}]'
        ")"
        for canary_name in canary_names
    )


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create the Playwright canary alert rule group."""
    alerting.RuleGroup(
        "playwright-canaries",
        name="playwright-canaries",
        folder_uid=folder_uid,
        interval_seconds=300,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="CanaryJourneyFailing",
                condition="C",
                # ~2 consecutive failed runs at a 10-minute cadence; see the
                # module docstring on why this reads as it does.
                for_="15m",
                # A canary with no series at all is the *other* rule's job. If
                # this one also fired on NoData, every gap between 10-minute runs
                # would report as a broken journey.
                no_data_state="OK",
                exec_err_state="OK",
                labels={"channel": "devops-warnings"},
                annotations={
                    "description": (
                        "The {{ $labels.canary }} Playwright canary against"
                        " {{ $labels.target }} has failed its user journey on"
                        " consecutive runs. A real browser could not complete"
                        " the journey -- log in, then search -- so this is a"
                        " user-facing break rather than an availability blip."
                        " HTTP probes can still be green while this is red."
                        " Failure artifacts for the run, including a Playwright"
                        " trace, are at"
                        " s3://ol-eng-artifacts/canary-results/canary-{{"
                        " $labels.canary }}/. Open trace.zip with"
                        " https://trace.playwright.dev/ ."
                    ),
                },
                datas=rd(_journey_failing_expr()),
            ),
            alerting.RuleGroupRuleArgs(
                name="CanaryNotReporting",
                condition="C",
                for_="10m",
                # absent_over_time returns a series whenever the metric is
                # missing, so this rule going NoData means the query itself
                # failed to evaluate rather than the canary being silent.
                no_data_state="OK",
                exec_err_state="OK",
                labels={"channel": "devops-warnings"},
                annotations={
                    "description": (
                        "The {{ $labels.canary }} Playwright canary has pushed no"
                        f" result for {_ABSENCE_WINDOW}, so nothing is currently"
                        " checking that a user can complete its journey. This is"
                        " the canary being broken rather than the property --"
                        " check its canary-{{ $labels.canary }} pipeline in"
                        " Concourse. Likely causes are the pipeline being unset"
                        " or paused, the schedule resource not triggering, or the"
                        " task dying before it could push. A journey that merely"
                        " fails still reports, so this is not that."
                    ),
                },
                datas=rd(_not_reporting_expr(EXPECTED_CANARIES)),
            ),
        ],
        opts=resource_opts,
    )
