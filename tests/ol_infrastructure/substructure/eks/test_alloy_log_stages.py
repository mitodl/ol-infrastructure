"""Every stage.match selector must be one Alloy's LogQL parser accepts.

Alloy (v1.20.0) rejects backtick-quoted line filters, and one bad selector
fails the reload of the whole pod_logs pipeline, so every other stage
silently stays on the previous config.
"""

import json
import re
from collections.abc import Callable

import pytest

from ol_infrastructure.substructure.aws.eks import grafana

STAGE_CONFIGS = [
    grafana._apisix_cookie_metrics_alloy_config,
    grafana._keycloak_olapps_idp_login_redact_alloy_config,
    grafana._apisix_low_value_log_drop_alloy_config,
    grafana._log_proxy_access_log_drop_alloy_config,
    grafana._edxapp_waffle_no_request_drop_alloy_config,
]


@pytest.mark.parametrize("stage_config", STAGE_CONFIGS, ids=lambda f: f.__name__)
def test_selectors_use_no_backtick_strings(stage_config: Callable[[], str]) -> None:
    selectors = [
        # River string escapes are a subset of JSON's
        json.loads(f'"{raw}"')
        for raw in re.findall(r'selector\s*=\s*"((?:[^"\\]|\\.)*)"', stage_config())
    ]
    assert selectors
    for selector in selectors:
        assert "`" not in selector, selector
