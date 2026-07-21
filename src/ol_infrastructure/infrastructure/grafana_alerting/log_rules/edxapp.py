"""edxapp application log alert rules.

Source: grafana-alerts/loki-rules/edxapp-logs.yaml
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create edxapp log alert rule groups."""
    alerting.RuleGroup(
        "loki-edxapp-mitxonline-web-5m",
        name="mitxonline-web-5m",
        folder_uid=folder_uid,
        interval_seconds=300,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="MitxOnline500Errors",
                condition="C",
                for_="5m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "High rate of 500 errors on mitxonline-production.",
                },
                datas=rd(
                    'sum(count_over_time(({application="edxapp", environment="mitxonline-production"}'
                    " | json"
                    " | container_name=~`.*-caddy-.*`"
                    ' | line_format "{{.message}}"'
                    " | json"
                    " | status >= 500)[5m])) > 100"
                ),
            ),
        ],
        opts=resource_opts,
    )

    alerting.RuleGroup(
        "loki-edxapp-all-apps-general-15m",
        name="all-apps-general-15m",
        folder_uid=folder_uid,
        interval_seconds=900,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="RedisMemoryIssuesProduction",
                condition="C",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "Redis is returning OOM errors to edxapp in {{ $labels.environment }}.",
                    "resolution": "Check on the {{ $labels.environment }} redis cluster in the AWS console and resize if needed.",
                },
                datas=rd(
                    'sum by(application,environment)(count_over_time({application="edxapp", environment=~".*production"}'
                    ' |json |= "OOM command not allowed when used memory"[15m]) >= 1)'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="RedisMemoryIssuesNonProd",
                condition="C",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "Redis is returning OOM errors to edxapp in {{ $labels.environment }}.",
                    "resolution": "Check on the {{ $labels.environment }} redis cluster in the AWS console and resize if needed.",
                },
                datas=rd(
                    'sum by(application,environment)(count_over_time({application="edxapp", environment!~".*production"}'
                    ' |json |= "OOM command not allowed when used memory"[15m]) >= 1)'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="CredentialIssueProduction",
                condition="C",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "A credential issue has been detected in {{ $labels.environment }}.",
                    "resolution": "Check on the {{ $labels.environment }} edxapp instances and initiate an instance refresh if needed",
                },
                datas=rd(
                    'sum by(application,environment)(count_over_time({application="edxapp", environment=~".*production"}'
                    ' |json |= "Access denied for user"[15m]) >= 1)'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="CredentialIssueNonProd",
                condition="C",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "A credential issue has been detected in {{ $labels.environment }}.",
                    "resolution": "Check on the {{ $labels.environment }} edxapp instances and initiate an instance refresh if needed",
                },
                datas=rd(
                    'sum by(application,environment)(count_over_time({application="edxapp", environment!~".*production"}'
                    ' |json |= "Access denied for user"[15m]) >= 1)'
                ),
            ),
        ],
        opts=resource_opts,
    )

    alerting.RuleGroup(
        "loki-edxapp-all-apps-general-1h",
        name="all-apps-general-1h",
        folder_uid=folder_uid,
        interval_seconds=3600,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="ForumTimeoutProduction",
                condition="C",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "edxapp {{ $labels.environment }} is having trouble communicating with the forum service.",
                    "resolution": "This typically means that forum is having difficulty talking to MongoAtlas. Check on forum containers and initiate an instance refresh if necessary.",
                },
                datas=rd(
                    'sum by (application, environment) (count_over_time({environment=~".*production", application="edxapp"}'
                    ' |json  |= "Read timed out" |="forum"[1h])) >= 1'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="ForumTimeoutNonProd",
                condition="C",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "edxapp {{ $labels.environment }} is having trouble communicating with the forum service.",
                    "resolution": "This typically means that forum is having difficulty talking to MongoAtlas. Check on forum containers and initiate an instance refresh if necessary.",
                },
                datas=rd(
                    'sum by (application, environment) (count_over_time({environment!~".*production", application="edxapp"}'
                    ' |json  |= "Read timed out" |="forum"[1h])) >= 1'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="NoSAMLProviderDataProduction",
                condition="C",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "A SAML configuration error has been detected in {{ $labels.application }} {{ $labels.environment }}.",
                    "resolution": (
                        "ssh into an instance and execute the following\n"
                        "sudo su - edxapp -s /bin/bash\n"
                        "source edxapp_env\n"
                        "python edx-platform/manage.py lms saml --pull\n"
                        "Takes several minutes to resolve\n"
                    ),
                },
                datas=rd(
                    '(sum by(application,environment)(count_over_time({environment=~".*production", application="edxapp"}'
                    ' |= "No SAMLProviderData found for provider"[1h])) > 1)'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="NoSAMLProviderDataNonProd",
                condition="C",
                for_="1m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": "A SAML configuration error has been detected in {{ $labels.application }} {{ $labels.environment }}.",
                    "resolution": (
                        "ssh into an instance and execute the following\n"
                        "sudo su - edxapp -s /bin/bash\n"
                        "source edxapp_env\n"
                        "python edx-platform/manage.py lms saml --pull\n"
                        "Takes several minutes to resolve\n"
                    ),
                },
                datas=rd(
                    '(sum by(application,environment)(count_over_time({environment!~".*production", application="edxapp"}'
                    ' |= "No SAMLProviderData found for provider"[1h])) > 1)'
                ),
            ),
        ],
        opts=resource_opts,
    )
