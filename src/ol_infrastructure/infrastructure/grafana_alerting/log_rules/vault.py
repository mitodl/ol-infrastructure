"""Vault secret and authentication failure log alert rules.

Source: grafana-alerts/loki-rules/vault.yaml
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create Vault log alert rule groups."""
    alerting.RuleGroup(
        "loki-vault-general",
        name="vault-general",
        folder_uid=folder_uid,
        interval_seconds=300,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="edxapp_VaultSecretsAbsent",
                condition="B",
                for_="5m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "A Vault or Consul template is attempting to retrieve a secret that doesn't exist. Investigate the possibility of a misconfiguration or an accidentally deleted value.",
                },
                datas=rd(
                    'count by (environment) (count_over_time({application="edxapp"}'
                    ' | json | SYSLOG_IDENTIFIER="vault" |~ "no secret exists"[5m])) > 0'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="edxapp_VaultAuthFailure",
                condition="B",
                for_="5m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": "A Vault client is having errors authenticating against the vault servers.",
                },
                datas=rd(
                    'count by (environment) (count_over_time({application="edxapp"}'
                    ' | json | SYSLOG_IDENTIFIER="vault" |~ "error authenticating"[5m])) > 0'
                ),
            ),
        ],
        opts=resource_opts,
    )
