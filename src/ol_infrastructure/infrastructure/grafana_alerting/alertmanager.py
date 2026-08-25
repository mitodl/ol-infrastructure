"""Grafana Alertmanager contact points and notification policy.

Translates grafana-alerts/alertmanager.yaml into Pulumi-managed resources.

Routing logic (mirrors the original alertmanager.yaml route tree):
  1. Alerts labelled channel=devops-warnings go to a separate pair of Slack
     channels by severity -- for alerts that aren't any one team's concern
     (e.g. cluster-wide capacity signals).
  2. Alerts whose name matches Kube.* are silenced (built-in k8s noise).
  3. Pod(OOMKilled|CrashLooping)(Warning|Critical) → Rootly, but grouped at
     the namespace level (not pod/container) with a longer group_interval,
     to stop a churning workload from minting one alert per pod name.
  4. All remaining warning-severity alerts → Rootly.
  5. All remaining critical-severity alerts → Rootly.
  6. Everything else → oblivion (default receiver, acts as a drop sink).
"""

from typing import Any

from pulumi import ResourceOptions
from pulumiverse_grafana import alerting


def create(grafana_secrets: dict[str, Any], resource_opts: ResourceOptions) -> None:
    """Create Grafana Alertmanager contact points and the notification policy."""
    # -------------------------------------------------------------------------
    # Contact points
    # -------------------------------------------------------------------------

    # Drop sink — used as the default receiver and to explicitly silence
    # matched routes. Grafana-managed contact points require at least one
    # integration (the old Mimir Alertmanager allowed an empty receiver), so
    # point a webhook at a blackhole address: delivery fails immediately and
    # the alert goes nowhere, which is the intent.
    alerting.ContactPoint(
        "oblivion",
        name="oblivion",
        webhooks=[
            alerting.ContactPointWebhookArgs(
                url="http://127.0.0.1:9/oblivion",
                disable_resolve_message=True,
            )
        ],
        opts=resource_opts,
    )

    # Rootly — all actionable warning + critical alerts route here via webhook.
    alerting.ContactPoint(
        "rootly",
        name="rootly",
        webhooks=[
            alerting.ContactPointWebhookArgs(
                url="https://webhooks.rootly.com/webhooks/incoming/alertmanager_webhooks",
                authorization_scheme="Bearer",
                authorization_credentials=grafana_secrets["rootly_bearer_token"],
                disable_resolve_message=False,
            )
        ],
        opts=resource_opts,
    )

    # devops-warnings Slack — non-paging visibility for cluster-wide capacity
    # signals that aren't any one team's concern (e.g. HPAAtMaxReplicas*,
    # which fires for every CI/QA/production workload).
    alerting.ContactPoint(
        "slack-devops-warnings-warning",
        name="slack-devops-warnings-warning",
        slacks=[
            alerting.ContactPointSlackArgs(
                url=grafana_secrets["slack_notifications_devops_warnings"],
                recipient="#devops-warnings",
                color="warning",
                icon_emoji=":goose_warning:",
                title=':goose_warning: [{{ .Status | toUpper }}{{ if eq .Status "firing" }}:{{ .Alerts.Firing | len }}{{- end }}] - {{ .CommonLabels.alertname }}',
                text="{{ range .Alerts }}\n  {{- if .Annotations.message }}\n      Message - {{ .Annotations.message }}\n  {{- end }}\n  {{- if .Annotations.description }}\n      Description - {{ .Annotations.description }}\n  {{- end }}\n  {{- if .Annotations.summary }}\n      Summary - {{ .Annotations.summary }}\n  {{- end }}\n{{- end }}",
                disable_resolve_message=False,
            )
        ],
        opts=resource_opts,
    )

    alerting.ContactPoint(
        "slack-devops-warnings-critical",
        name="slack-devops-warnings-critical",
        slacks=[
            alerting.ContactPointSlackArgs(
                url=grafana_secrets["slack_notifications_devops_warnings"],
                recipient="#devops-warnings",
                color="danger",
                title=':alert: [{{ .Status | toUpper }}{{ if eq .Status "firing" }}:{{ .Alerts.Firing | len }}{{- end }}] - {{ .CommonLabels.alertname }}',
                text="{{ range .Alerts }}\n  {{- if .Annotations.message }}\n      {{ .Annotations.message }}\n  {{- end }}\n  {{- if .Annotations.description }}\n      {{ .Annotations.description }}\n  {{- end }}\n{{- end }}",
                disable_resolve_message=False,
            )
        ],
        opts=resource_opts,
    )

    # -------------------------------------------------------------------------
    # Notification policy (route tree)
    # -------------------------------------------------------------------------
    alerting.NotificationPolicy(
        "grafana-notification-policy",
        contact_point="oblivion",
        # Grouping by alertname (+ environment, which only log_rules-based
        # alerts carry -- metric_rules ones use `cluster` instead) bundles
        # every resource a rule can match into one notification thread. Most
        # rules here match many independent resources at once (any pod, any
        # HPA, any node, any deployment, ... cluster-wide, sometimes across
        # multiple real clusters via regex), so one resource changing state
        # resends the whole bundle and sweeps in every other still-firing
        # resource under the same rule, even though nothing about them
        # changed (observed 2026-07-23: an apisix HPA alert firing
        # continuously since the day before kept reappearing in
        # notifications purely because an unrelated HPA in another
        # namespace kept flapping). Adding every resource-identifying label
        # used across metric_rules and log_rules gives each distinct
        # resource its own notification thread, only re-notified when that
        # specific resource's own state changes. A label absent from a given
        # alert (e.g. `pod` on an HPA alert) is harmless -- Alertmanager
        # treats it as empty for grouping, so each rule naturally groups
        # down to whichever of these labels it actually carries.
        #
        # Trade-off: this also splits apart genuinely-correlated alerts that
        # used to bundle by coincidence (e.g. several unrelated pods
        # OOMKilled by the same root cause at the same instant no longer
        # arrive as one grouped message) -- accepted in exchange for no
        # longer bundling truly-unrelated resources together.
        group_bies=[
            "alertname",
            "environment",
            "cluster",
            "namespace",
            "application",
            "pod",
            "container",
            "deployment",
            "statefulset",
            "daemonset",
            "horizontalpodautoscaler",
            "node",
            "job_name",
            "instance",
            # The edge-level equivalent of the labels above: metric_rules/
            # apisix_edge.py aggregates `sum by (matched_host)`, so this is the
            # only resource-identifying label its alerts carry. Without it every
            # firing host collapses into one notification group per rule, which
            # is precisely the bundling this list exists to prevent. Added
            # ahead of those rules being routed anywhere, so that promoting them
            # really is only a matter of adding a `severity` label.
            "matched_host",
            # Same reasoning for the CronJob staleness rules in
            # metric_rules/eks_general.py, which aggregate `max by (cluster,
            # namespace, cronjob)`. `job_name` above identifies an individual Job
            # (`<cronjob>-<timestamp>`); a stale CronJob never produces one, so
            # without `cronjob` here every stalled schedule in a cluster would
            # arrive as a single grouped notification.
            "cronjob",
        ],
        # "1m", not "60s" — Grafana normalizes durations to the largest unit and
        # a mismatched spelling shows as a perpetual diff on every preview.
        group_wait="1m",
        group_interval="5m",
        repeat_interval="4h",
        policies=[
            # devops-warnings: for alerts that aren't any one team's concern
            # (e.g. HPAAtMaxReplicas* in metric_rules/eks_general.py, which
            # fires for every workload cluster-wide).
            alerting.NotificationPolicyPolicyArgs(
                matchers=[
                    alerting.NotificationPolicyPolicyMatcherArgs(
                        label="channel",
                        match="=",
                        value="devops-warnings",
                    )
                ],
                contact_point="oblivion",
                continue_=False,
                policies=[
                    alerting.NotificationPolicyPolicyPolicyArgs(
                        matchers=[
                            alerting.NotificationPolicyPolicyPolicyMatcherArgs(
                                label="severity",
                                match="=",
                                value="warning",
                            )
                        ],
                        contact_point="slack-devops-warnings-warning",
                        continue_=False,
                    ),
                    alerting.NotificationPolicyPolicyPolicyArgs(
                        matchers=[
                            alerting.NotificationPolicyPolicyPolicyMatcherArgs(
                                label="severity",
                                match="=",
                                value="critical",
                            )
                        ],
                        contact_point="slack-devops-warnings-critical",
                        continue_=False,
                    ),
                ],
            ),
            # Silence built-in Kubernetes alerts — too noisy, not actionable.
            alerting.NotificationPolicyPolicyArgs(
                matchers=[
                    alerting.NotificationPolicyPolicyMatcherArgs(
                        label="alertname",
                        match="=~",
                        value="Kube.*",
                    )
                ],
                contact_point="oblivion",
                continue_=False,
            ),
            # Pod OOM/crash-loop storms: override the root grouping instead of
            # reverting it. The root group_bies above deliberately includes
            # `pod`/`container` so unrelated resources get their own
            # notification thread, but for these two rule families that same
            # granularity is the storm mechanism -- a churning workload mints
            # a new pod name per restart, and the root grouping then mints a
            # new Rootly alert per name (296 PodOOMKilledCritical firings in
            # 30 days from what was, in practice, one workload). Grouping at
            # the namespace level here reports the workload once. Sits above
            # the severity routes so it applies before either one, and still
            # ends at the same `rootly` contact point.
            #
            # Grouping on `deployment` collapses to namespace level as
            # written -- these rules aggregate by (cluster, namespace, pod,
            # container) and emit no `deployment` label. Accepted rather than
            # adding a kube_pod_owner/kube_replicaset_owner join: the
            # storm this fixes was one deployment in one namespace, so
            # namespace-level grouping already reports it as intended. See
            # docs/plans/grafana-alerting-remediation-spec.md §3b.
            alerting.NotificationPolicyPolicyArgs(
                matchers=[
                    alerting.NotificationPolicyPolicyMatcherArgs(
                        label="alertname",
                        match="=~",
                        value="Pod(OOMKilled|CrashLooping)(Warning|Critical)",
                    )
                ],
                contact_point="rootly",
                continue_=False,
                group_bies=[
                    "alertname",
                    "grafana_folder",
                    "cluster",
                    "namespace",
                    "deployment",
                ],
                group_interval="30m",
                repeat_interval="12h",
            ),
            # All warning-severity alerts → Rootly.
            alerting.NotificationPolicyPolicyArgs(
                matchers=[
                    alerting.NotificationPolicyPolicyMatcherArgs(
                        label="severity",
                        match="=",
                        value="warning",
                    )
                ],
                contact_point="rootly",
                continue_=False,
            ),
            # All critical-severity alerts → Rootly.
            alerting.NotificationPolicyPolicyArgs(
                matchers=[
                    alerting.NotificationPolicyPolicyMatcherArgs(
                        label="severity",
                        match="=",
                        value="critical",
                    )
                ],
                contact_point="rootly",
                continue_=False,
            ),
        ],
        opts=resource_opts,
    )
