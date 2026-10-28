"""Adopt the olapps realm only when Keycloak says it exists, and only the realm."""

import asyncio
import importlib.util
from pathlib import Path

import httpx
import pulumi
import pulumi_keycloak
import pulumi_kubernetes
import pytest

# Python 3.14+ compatibility, as tests/local_dev/test_mitxonline_program.py does.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

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


class _RealmMocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.import_ids: dict[tuple[str, str], str | None] = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.import_ids[(args.typ, args.name)] = args.resource_id
        return [args.resource_id or f"{args.name}-id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):
        # The OVS Secret reads the realm's first RS256 key.
        if args.token == "keycloak:index/getRealmKeys:getRealmKeys":  # noqa: S105
            return {"keys": [{"publicKey": "k"}]}
        return {}


def _realm_import_id(realm_import_id: str | None) -> str | None:
    """Build the realm under mocks; return the import ID the Realm registered with."""
    mocks = _RealmMocks()
    # Set per run, not at import: other test modules set their own mocks at
    # import time and the last one wins.
    pulumi.runtime.set_mocks(
        mocks, project="ol-local-dev-apps-infra", stack="test", preview=False
    )

    @pulumi.runtime.test
    def run():
        keycloak_module.create_olapps_dev_realm(
            keycloak_provider=pulumi_keycloak.Provider("kc", url=URL),
            keycloak_url=URL,
            k8s_provider=pulumi_kubernetes.Provider("k8s"),
            mitlearn_client_secret=pulumi.Output.secret("a"),
            learn_ai_client_secret=pulumi.Output.secret("b"),
            mitxonline_client_secret=pulumi.Output.secret("c"),
            unified_ecommerce_client_secret=pulumi.Output.secret("d"),
            ovs_client_secret=pulumi.Output.secret("e"),
            ocw_studio_client_secret=pulumi.Output.secret("f"),
            root_domain="mit.test",
            realm_import_id=realm_import_id,
        )
        return pulumi.Output.from_input(True)  # noqa: FBT003

    run()
    return mocks.import_ids[("keycloak:index/realm:Realm", "olapps")]


def test_found_realm_is_registered_as_an_import() -> None:
    assert _realm_import_id("olapps") == "olapps"


def test_missing_realm_is_registered_as_a_create() -> None:
    assert not _realm_import_id(None)
