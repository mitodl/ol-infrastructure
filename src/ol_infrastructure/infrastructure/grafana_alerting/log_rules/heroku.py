"""Heroku application log alert rules.

Source: grafana-alerts/loki-rules/heroku-logs.yaml

Note: two rules have been adjusted from the original YAML:
- OCWStudioContentSyncInvalidPassword{NonProd,Prod}: > 0 appended because the
  original expression had no threshold, which would cause Stage B to fire even
  when the count is 0 (Stage A returns a row with value 0 instead of no rows).
- Keycloak rules: bare log stream queries wrapped in count_over_time([5m]) > 0
  to make them metric-producing so the two-stage pipeline works correctly.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create Heroku application log alert rule groups."""
    alerting.RuleGroup(
        "loki-heroku-all-apps-general",
        name="all-apps-general",
        folder_uid=folder_uid,
        interval_seconds=1800,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="InvalidAccessKeyProduction",
                condition="B",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "An invalid aws key has been detected in {{ $labels.application }} {{ $labels.environment }}.",
                },
                datas=rd(
                    'sum by (application, environment) (count_over_time({environment=~".*production", application=~".+", application!="dagster", application!="airbyte"}'
                    ' | json |= "InvalidAccessKeyId"[3h])) >= 1'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="InvalidAccessKeyNonProd",
                condition="B",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "An invalid aws key has been detected in {{ $labels.application }} {{ $labels.environment }}.",
                },
                datas=rd(
                    'sum by (application, environment) (count_over_time({environment!~".*production", application=~".+", application!="dagster", application!="airbyte"}'
                    ' | json |= "InvalidAccessKeyId"[3h])) >= 1'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="AlternateInvalidAccessKeyProduction",
                condition="B",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "An invalid aws key has been detected in {{ $labels.application }} {{ $labels.environment }}.",
                },
                datas=rd(
                    'sum by(application, environment) (count_over_time({environment=~".*production", application=~".+", application!="airbyte"}'
                    ' |= "An error occurred (403) when calling the HeadObject operation: Forbidden" [3h])) >=1'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="AlternateInvalidAccessKeyNonProd",
                condition="B",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "An invalid aws key has been detected in {{ $labels.application }} {{ $labels.environment }}.",
                },
                datas=rd(
                    'sum by(application, environment) (count_over_time({environment!~".*production", application=~".+", application!="airbyte"}'
                    ' |= "An error occurred (403) when calling the HeadObject operation: Forbidden" [3h])) >=1'
                ),
            ),
        ],
        opts=resource_opts,
    )

    alerting.RuleGroup(
        "loki-heroku-bootcamps",
        name="bootcamps",
        folder_uid=folder_uid,
        interval_seconds=1800,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="BootcampsSAMLIntegrationErrorProd",
                condition="B",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "The Bootcamps authentication integration with NovoEd is broken. This prevents learners from accessing courses.",
                },
                datas=rd(
                    'sum by(application, environment) (count_over_time({environment=~".*production", application=~"bootcamp-ecommerce"}'
                    ' |= "Unable to refresh local metadata" [3h])) >=1'
                ),
            ),
        ],
        opts=resource_opts,
    )

    alerting.RuleGroup(
        "loki-heroku-ocw-studio",
        name="ocw-studio",
        folder_uid=folder_uid,
        interval_seconds=300,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="OCWStudio3PlayErrorDetectedNonProd",
                condition="B",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "warning", "channel": "notifications-ocw-misc"},
                annotations={
                    "description": "A 3play transcript request has failed in NonProduction.",
                },
                datas=rd(
                    'sum by (application, environment) (count_over_time({service="heroku", application="ocw-studio", environment!~".*production"}'
                    ' |= "3Play transcript request failed for video_id" [5m])) >= 1'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="OCWStudio3PlayErrorDetectedProd",
                condition="B",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "warning", "channel": "notifications-ocw-misc"},
                annotations={
                    "description": "A 3play transcript request has failed in Production.",
                },
                datas=rd(
                    'sum by (application, environment) (count_over_time({service="heroku", application="ocw-studio", environment=~".*production"}'
                    ' |= "3Play transcript request failed for video_id" [5m])) >= 1'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="OCWStudioContentSyncInvalidPasswordNonProd",
                condition="B",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "An invalid username and password message was detected from content sync. This refers to the password used by OCW to talk to concourse.",
                },
                datas=rd(
                    'sum by (application, environment) (count_over_time({application="ocw-studio", environment!~".*production"}'
                    ' | json | message="Invalid Username and Password"[5m])) > 0'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="OCWStudioContentSyncInvalidPasswordProd",
                condition="B",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "An invalid username and password message was detected from content sync. This refers to the password used by OCW to talk to concourse.",
                },
                datas=rd(
                    'sum by (application, environment) (count_over_time({application="ocw-studio", environment=~".*production"}'
                    ' | json | message="Invalid Username and Password"[5m])) > 0'
                ),
            ),
        ],
        opts=resource_opts,
    )

    alerting.RuleGroup(
        "loki-heroku-keycloak",
        name="keycloak",
        folder_uid=folder_uid,
        interval_seconds=60,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="KeycloakInternalError",
                condition="B",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "Keycloak has responded with a 500 error which is likely caused by a customization or configuration change.",
                },
                datas=rd(
                    'count_over_time({application="keycloak"} |= "HTTP 500 Internal Server Error" [5m]) > 0'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="KeycloakSAMLAssertionDecryptError",
                condition="B",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "Keycloak is unable to decrypt a SAML assertion likely caused by an internal or external configuration change.",
                },
                datas=rd(
                    'count_over_time({application="keycloak"} |= "Not possible to decrypt SAML assertion" [5m]) > 0'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="KeycloakServerError",
                condition="B",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "Keycloak experienced an error condition that is likely the result of a customization.",
                },
                datas=rd(
                    'count_over_time({application="keycloak"} |= "Uncaught server error" [5m]) > 0'
                ),
            ),
        ],
        opts=resource_opts,
    )
