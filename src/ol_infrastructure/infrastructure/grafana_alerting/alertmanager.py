"""Grafana Alertmanager contact points and notification policy.

Translates grafana-alerts/alertmanager.yaml into Pulumi-managed resources.

Routing logic (mirrors the original alertmanager.yaml route tree):
  1. Alerts labelled channel=notifications-ocw-misc go to dedicated Slack
     channels by severity; anything else with that channel label is silenced.
  2. Alerts whose name matches Kube.* are silenced (built-in k8s noise).
  3. Warning-severity alerts:
       - during the day (09:00-17:00 America/New_York) → Rootly (pages).
       - overnight (17:00-09:00) → Slack only, so transient warnings that
         self-resolve do not page a human in the middle of the night while
         still surfacing the spike for awareness.
  4. All critical-severity alerts → Rootly, 24/7 (unchanged).
  5. Everything else → oblivion (default receiver, acts as a drop sink).

The overnight/daytime split is implemented with two complementary mute timings
and a continue=True on the daytime Rootly route: a warning is matched by both
the Rootly route and the Slack route, and whichever one is muted for the
current time drops out, leaving exactly one active destination.
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

    # On-call overnight Slack — warning-severity alerts that fire outside
    # business hours land here instead of paging Rootly. Preserves awareness of
    # a spike without waking someone for something that usually self-resolves.
    alerting.ContactPoint(
        "slack-devops-alerts-overnight-warning",
        name="slack-devops-alerts-overnight-warning",
        slacks=[
            alerting.ContactPointSlackArgs(
                url=grafana_secrets["slack_devops_alerts_overnight_api_url"],
                recipient="#devops-alerts",
                color="warning",
                icon_emoji=":goose_warning:",
                title=':goose_warning: [OVERNIGHT] [{{ .Status | toUpper }}{{ if eq .Status "firing" }}:{{ .Alerts.Firing | len }}{{- end }}] - {{ .CommonLabels.alertname }}',
                text="{{ range .Alerts }}\n  {{- if .Annotations.message }}\n      Message - {{ .Annotations.message }}\n  {{- end }}\n  {{- if .Annotations.description }}\n      Description - {{ .Annotations.description }}\n  {{- end }}\n  {{- if .Annotations.summary }}\n      Summary - {{ .Annotations.summary }}\n  {{- end }}\n{{- end }}",
                disable_resolve_message=False,
            )
        ],
        opts=resource_opts,
    )

    # -------------------------------------------------------------------------
    # Mute timings — the overnight/daytime split for warning-severity routing.
    # Times are local to America/New_York, so the window tracks EST/EDT
    # automatically. Alertmanager time ranges cannot span midnight, so the
    # overnight window is expressed as two ranges (00:00-09:00 and 17:00-24:00).
    # -------------------------------------------------------------------------
    mute_overnight = alerting.MuteTiming(
        "mute-overnight",
        name="overnight-17-to-09-eastern",
        intervals=[
            alerting.MuteTimingIntervalArgs(
                location="America/New_York",
                times=[
                    alerting.MuteTimingIntervalTimeArgs(start="00:00", end="09:00"),
                    alerting.MuteTimingIntervalTimeArgs(start="17:00", end="24:00"),
                ],
            )
        ],
        opts=resource_opts,
    )

    mute_daytime = alerting.MuteTiming(
        "mute-daytime",
        name="daytime-09-to-17-eastern",
        intervals=[
            alerting.MuteTimingIntervalArgs(
                location="America/New_York",
                times=[
                    alerting.MuteTimingIntervalTimeArgs(start="09:00", end="17:00"),
                ],
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
        group_bies=["alertname", "environment"],
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
            # Warning-severity alerts, daytime (09:00-17:00 Eastern) → Rootly.
            # Muted overnight so it does not page. continue_=True lets the same
            # alert also reach the overnight Slack route below; whichever route
            # is muted for the current time simply produces no notification.
            alerting.NotificationPolicyPolicyArgs(
                matchers=[
                    alerting.NotificationPolicyPolicyMatcherArgs(
                        label="severity",
                        match="=",
                        value="warning",
                    )
                ],
                contact_point="rootly",
                mute_timings=[mute_overnight.name],
                continue_=True,
            ),
            # Warning-severity alerts, overnight (17:00-09:00 Eastern) → Slack
            # only. Muted during the day, when the Rootly route above pages.
            alerting.NotificationPolicyPolicyArgs(
                matchers=[
                    alerting.NotificationPolicyPolicyMatcherArgs(
                        label="severity",
                        match="=",
                        value="warning",
                    )
                ],
                contact_point="slack-devops-alerts-overnight-warning",
                mute_timings=[mute_daytime.name],
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
