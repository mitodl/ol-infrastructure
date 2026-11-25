"""Pin local-dev's SCIM remote providers to the deployed definitions."""

import importlib
import sys
from pathlib import Path

import pytest
import yaml

from bridge.lib.magic_numbers import DEFAULT_WSGI_PORT
from ol_infrastructure.applications.mitxonline import definition
from ol_infrastructure.providers.keycloak_scim import ScimRemoteProviderSpec
from tests.local_dev.test_mitxonline_program import _any_bindings

REPO = Path(__file__).parents[2]
INFRA_DIR = REPO / "local-dev/infra"
PRODUCTION_STACK = (
    REPO / "src/ol_infrastructure/substructure/keycloak/Pulumi.Production.yaml"
)


@pytest.fixture
def keycloak_scim(monkeypatch):
    monkeypatch.syspath_prepend(str(INFRA_DIR))
    for name in [m for m in sys.modules if m == "modules" or m.startswith("modules.")]:
        monkeypatch.delitem(sys.modules, name)
    return importlib.import_module("modules.keycloak_scim")


def _production_providers() -> dict[str, ScimRemoteProviderSpec]:
    entries = yaml.safe_load(PRODUCTION_STACK.read_text())["config"][
        "keycloak_scim:remote_providers"
    ]
    return {
        entry["name"]: ScimRemoteProviderSpec.model_validate(
            {k: v for k, v in entry.items() if k != "authentication_list"}
        )
        for entry in entries
    }


def test_mitxonline_url_uses_the_port_the_component_serves(keycloak_scim):
    """OLApplicationK8s serves DEFAULT_WSGI_PORT only without nginx or a port."""
    config = definition.application_config(_any_bindings())

    assert config.import_nginx_config is False
    assert config.application_port is None
    assert config.application_lb_service_name == definition.WEBAPP_SERVICE_NAME
    assert (
        f"http://{definition.WEBAPP_SERVICE_NAME}.{definition.NAMESPACE}"
        f".svc.cluster.local:{DEFAULT_WSGI_PORT}/scim/v2"
    ) == keycloak_scim.MITXONLINE_SCIM_BASE_URL


@pytest.mark.parametrize(
    ("name", "attribute"),
    [
        ("ol-learn-scim", "LEARN_PROVIDER"),
        ("ol-mitxonline-scim", "MITXONLINE_PROVIDER"),
    ],
)
def test_local_providers_send_what_production_sends(keycloak_scim, name, attribute):
    """Every flag and mapping Production declares is the same locally."""
    production = _production_providers()[name]
    local = ScimRemoteProviderSpec(
        realm="olapps",
        name=name,
        base_url="http://local/scim/v2",
        enabled=True,
        **getattr(keycloak_scim, attribute),
    )
    shared = {
        key
        for key, value in production.model_dump().items()
        if value is not None
        and key not in {"realm", "name", "base_url", "enabled"}
        and key not in {"replay_on_failure", "retry_interval_in_seconds"}
    }

    assert shared
    assert {k: getattr(local, k) for k in shared} == {
        k: getattr(production, k) for k in shared
    }
