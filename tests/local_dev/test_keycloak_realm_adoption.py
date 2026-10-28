"""live_realm_import_id: adopt the realm only when Keycloak says it exists."""

import importlib.util
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).parents[2]
_spec = importlib.util.spec_from_file_location(
    "local_dev_keycloak", REPO / "local-dev/infra/modules/keycloak.py"
)
assert _spec
assert _spec.loader
keycloak_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(keycloak_module)

URL = "https://sso.ol.mit.test"


def _keycloak(
    monkeypatch: pytest.MonkeyPatch, *, token_status: int, realm_status: int
) -> list[str]:
    """Route the helper's httpx.Client to a fake Keycloak; return requested paths."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/realms/master/protocol/openid-connect/token":
            return httpx.Response(token_status, json={"access_token": "t"})
        assert request.headers["Authorization"] == "Bearer t"
        return httpx.Response(realm_status, json={})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    return seen


def test_existing_realm_is_imported(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _keycloak(monkeypatch, token_status=200, realm_status=200)
    assert (
        keycloak_module.live_realm_import_id(URL, "olapps", "admin", "admin")
        == "olapps"
    )
    assert seen[-1] == "/admin/realms/olapps"


def test_missing_realm_is_created(monkeypatch: pytest.MonkeyPatch) -> None:
    _keycloak(monkeypatch, token_status=200, realm_status=404)
    assert keycloak_module.live_realm_import_id(URL, "olapps", "admin", "admin") is None


@pytest.mark.parametrize(
    ("token_status", "realm_status"), [(401, 200), (200, 403), (200, 500)]
)
def test_unanswered_lookup_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, token_status: int, realm_status: int
) -> None:
    # Treating these as "no realm" would send Pulumi back to the 409 it is
    # meant to avoid, so they surface instead.
    _keycloak(monkeypatch, token_status=token_status, realm_status=realm_status)
    with pytest.raises(httpx.HTTPStatusError):
        keycloak_module.live_realm_import_id(URL, "olapps", "admin", "admin")
