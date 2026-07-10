"""Grafana Cloud alerting + Pingdom uptime checks — Pulumi entry point.

Bootstraps the Grafana provider and delegates to submodules:
  alertmanager  — contact points + notification policy (all stacks)
  metric_rules  — Prometheus/Mimir metric alert rule groups (all stacks)
  log_rules     — Loki log-based alert rule groups (all stacks)
  pingdom_checks — Pingdom uptime checks via dynamic provider (production stack only)

See CLAUDE.md in this directory for a full description of the architecture.
"""

from pathlib import Path

import pulumiverse_grafana as grafana
from pulumi import Output, ResourceOptions

from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.infrastructure.grafana_alerting import (
    alertmanager,
    log_rules,
    metric_rules,
    pingdom_checks,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()
is_production = stack_info.env_suffix == "production"

grafana_secrets = read_yaml_secrets(
    Path(f"grafana_cloud/api.{stack_info.env_suffix}.yaml")
)

grafana_provider = grafana.Provider(
    "grafana-provider",
    url=grafana_secrets["grafana_url"],
    auth=grafana_secrets["grafana_api_token"],
)

resource_opts = ResourceOptions(provider=grafana_provider)

alertmanager.create(grafana_secrets, resource_opts)
metric_rules.create(resource_opts)
log_rules.create(resource_opts)

# Pingdom checks are account-wide — only create from the production stack.
if is_production:
    pingdom_checks.create(
        api_token=Output.secret(grafana_secrets["pingdom_api_token"]),
        integration_ids=grafana_secrets.get("pingdom_integration_ids", []),
    )
