"""Grafana Alertmanager contact points and notification policy.

Translates grafana-alerts/alertmanager.yaml into Pulumi-managed resources.

Routing logic (mirrors the original alertmanager.yaml route tree):
  1. Alerts labelled channel=notifications-ocw-misc go to dedicated Slack
     channels by severity; anything else with that channel label is silenced.
  2. Alerts whose name matches Kube.* are silenced (built-in k8s noise).
  3. All remaining warning-severity alerts → Rootly.
  4. All remaining critical-severity alerts → Rootly.
  5. Everything else → oblivion (default receiver, acts as a drop sink).
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

    # OCW misc Slack — warning severity (yellow goose emoji, "warning" colour).
    alerting.ContactPoint(
        "slack-notifications-ocw-misc-warning",
        name="slack-notifications-ocw-misc-warning",
        slacks=[
            alerting.ContactPointSlackArgs(
                url=grafana_secrets["slack_notifications_ocw_misc_api_url"],
                recipient="#notifications-ocw-misc",
                color="warning",
                icon_emoji=":goose_warning:",
                title=':goose_warning: [{{ .Status | toUpper }}{{ if eq .Status "firing" }}:{{ .Alerts.Firing | len }}{{- end }}] - {{ .CommonLabels.alertname }}',
                text="{{ range .Alerts }}\n  {{- if .Annotations.message }}\n      Message - {{ .Annotations.message }}\n  {{- end }}\n  {{- if .Annotations.description }}\n      Description - {{ .Annotations.description }}\n  {{- end }}\n  {{- if .Annotations.summary }}\n      Summary - {{ .Annotations.summary }}\n  {{- end }}\n{{- end }}",
                disable_resolve_message=False,
            )
        ],
        opts=resource_opts,
    )

    # OCW misc Slack — critical severity (red alert emoji, "danger" colour).
    alerting.ContactPoint(
        "slack-notifications-ocw-misc-critical",
        name="slack-notifications-ocw-misc-critical",
        slacks=[
            alerting.ContactPointSlackArgs(
                url=grafana_secrets["slack_notifications_ocw_misc_api_url"],
                recipient="#notifications-ocw-misc",
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
        ],
        # "1m", not "60s" — Grafana normalizes durations to the largest unit and
        # a mismatched spelling shows as a perpetual diff on every preview.
        group_wait="1m",
        group_interval="5m",
        repeat_interval="4h",
        policies=[
            # OCW misc: route warning/critical to the dedicated Slack channel;
            # anything else tagged with this channel label is silenced by the
            # parent "oblivion" receiver so it never reaches Rootly.
            alerting.NotificationPolicyPolicyArgs(
                matchers=[
                    alerting.NotificationPolicyPolicyMatcherArgs(
                        label="channel",
                        match="=",
                        value="notifications-ocw-misc",
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
                        contact_point="slack-notifications-ocw-misc-warning",
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
                        contact_point="slack-notifications-ocw-misc-critical",
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
