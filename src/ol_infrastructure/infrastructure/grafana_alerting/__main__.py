"""Grafana Cloud alerting — Pulumi entry point.

Bootstraps the Grafana provider and delegates to submodules:
  alertmanager  — contact points + notification policy (all stacks)
  metric_rules  — Prometheus/Mimir alert rule groups (all stacks)
  sm_checks     — Synthetic Monitoring uptime checks (production stack only)

See CLAUDE.md in this directory for a full description of the architecture.
"""

from pathlib import Path

import pulumiverse_grafana as grafana
from pulumi import InvokeOptions, ResourceOptions

from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.infrastructure.grafana_alerting import (
    alertmanager,
    metric_rules,
    sm_checks,
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
    # SM credentials are only present in the production secrets file.
    sm_url=grafana_secrets.get("grafana_sm_url") if is_production else None,
    sm_access_token=grafana_secrets.get("grafana_sm_access_token")
    if is_production
    else None,
)

resource_opts = ResourceOptions(provider=grafana_provider)
invoke_opts = InvokeOptions(provider=grafana_provider)

alertmanager.create(grafana_secrets, resource_opts)
metric_rules.create(stack_info, resource_opts)

# SM checks run in the production stack only — see sm_checks.py docstring.
if is_production:
    sm_checks.create(invoke_opts, resource_opts)
