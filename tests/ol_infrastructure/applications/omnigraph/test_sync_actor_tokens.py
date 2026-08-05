"""Tests for the witan-users -> actor-token reconciliation.

The properties worth pinning are the ones whose failure is silent in
production: a token that gets re-minted when it should have been carried over
bounces omnigraph-server for every user, and a map that loses its service
entries breaks CI without anybody's request failing in a way that names the
cause.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any, ClassVar

import pytest

from ol_infrastructure.applications.omnigraph.scripts.sync_actor_tokens import (
    SyncError,
    derive_actor_id,
    is_service_account,
    read_token_map,
    realm_users,
    reconcile,
)

SERVICE_TOKENS = {"svc-witan-ci": "ci-token"}  # pragma: allowlist secret


def member(user_id: str, *, enabled: bool = True) -> dict[str, Any]:
    return {"id": user_id, "username": f"user-{user_id}", "enabled": enabled}


def service_account(client_id: str) -> dict[str, Any]:
    """Build a realm user as Keycloak represents a client's own service account."""
    return {
        "id": f"sa-{client_id}",
        "username": f"service-account-{client_id}",
        "enabled": True,
        "serviceAccountClientId": client_id,
    }


def test_derive_actor_id_matches_witan_core():
    # The uuid case is the one that actually occurs; the others pin the
    # sanitising that has to agree with witan_core.identity.derive_actor_id.
    assert (
        derive_actor_id("2b1f0b3e-4c5d-4e6f-8a9b-0c1d2e3f4a5b")
        == "act-2b1f0b3e-4c5d-4e6f-8a9b-0c1d2e3f4a5b"
    )
    assert derive_actor_id("Mixed.Case_Sub") == "act-mixed-case-sub"
    assert derive_actor_id("  padded  ") == "act-padded"


def test_derive_actor_id_rejects_all_punctuation():
    # An id of bare "act-" would collide with every other such claim.
    with pytest.raises(SyncError):
        derive_actor_id("...")


def test_reconcile_adds_members_and_keeps_service_entries():
    desired = reconcile(SERVICE_TOKENS, {}, [member("alice"), member("bob")])

    assert desired["svc-witan-ci"] == "ci-token"  # pragma: allowlist secret
    assert set(desired) == {"svc-witan-ci", "act-alice", "act-bob"}
    # Freshly minted, distinct, and not derived from anything guessable.
    assert desired["act-alice"] != desired["act-bob"]
    assert len(desired["act-alice"]) >= 32


def test_reconcile_is_a_noop_for_unchanged_membership():
    # The property the whole restart budget rests on: an unchanged membership
    # must produce a byte-identical map, so nothing is written and
    # omnigraph-server is not bounced.
    first = reconcile(SERVICE_TOKENS, {}, [member("alice")])
    second = reconcile(SERVICE_TOKENS, first, [member("alice")])

    assert second == first


def test_reconcile_retires_departed_and_disabled_members():
    current = reconcile(SERVICE_TOKENS, {}, [member("alice"), member("bob")])

    # bob left the group entirely; alice is still a member but disabled.
    desired = reconcile(SERVICE_TOKENS, current, [member("alice", enabled=False)])

    assert set(desired) == {"svc-witan-ci"}


def test_reconcile_carries_over_only_the_surviving_member():
    current = reconcile(SERVICE_TOKENS, {}, [member("alice"), member("bob")])

    desired = reconcile(SERVICE_TOKENS, current, [member("alice"), member("carol")])

    assert desired["act-alice"] == current["act-alice"]
    assert desired["act-carol"] not in current.values()
    assert "act-bob" not in desired


def test_reconcile_refuses_a_member_colliding_with_a_service_actor():
    # Would hand a human the service identity's token.
    with pytest.raises(SyncError, match="collides with a service actor"):
        reconcile({"act-alice": "svc"}, {}, [member("alice")])


def test_reconcile_rejects_a_member_with_no_id():
    with pytest.raises(SyncError, match="no id"):
        reconcile(SERVICE_TOKENS, {}, [{"username": "nameless"}])


def test_reconcile_skips_service_account_users():
    # Enumerating the realm returns clients' own service accounts alongside
    # people — this realm has one for ol-opik-client and one for the token-sync
    # client itself. Minting a human's interactive token for them would hand
    # every such client the Cedar rights of a person.
    desired = reconcile(
        SERVICE_TOKENS,
        {},
        [
            member("alice"),
            service_account("ol-opik-client"),
            service_account("witan-token-sync"),
        ],
    )

    assert set(desired) == {"svc-witan-ci", "act-alice"}


def test_is_service_account_detects_both_signals():
    assert is_service_account(service_account("ol-opik-client"))
    # Username convention alone, in case the representation omits the field.
    assert is_service_account({"username": "service-account-something"})
    assert not is_service_account(member("alice"))


class _StubHandler(BaseHTTPRequestHandler):
    """Serves whatever ``routes`` maps the path to: (status, body)."""

    routes: ClassVar[dict[str, Any]] = {}

    def do_GET(self):
        status, body = self.routes.get(self.path, (404, ""))
        payload = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def stub_server():
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_read_token_map_treats_a_missing_path_as_empty(stub_server):
    _, base = stub_server
    _StubHandler.routes = {}

    # The bootstrap case: this job's first run is what creates the path.
    assert read_token_map(base, "token", "secret-operations/witan/actor-tokens") == {}


def test_read_token_map_rejects_a_corrupt_payload(stub_server):
    _, base = stub_server
    _StubHandler.routes = {
        "/v1/secret-operations/witan/actor-tokens": (
            200,
            {"data": {"tokens_json": "{not json"}},
        )
    }

    # Continuing here would rewrite the path with a partial map.
    with pytest.raises(SyncError, match="not valid JSON"):
        read_token_map(base, "token", "secret-operations/witan/actor-tokens")


def test_read_token_map_rejects_an_existing_secret_with_no_key(stub_server):
    _, base = stub_server
    _StubHandler.routes = {
        "/v1/secret-operations/witan/actor-tokens": (200, {"data": {"other": "x"}})
    }

    # Distinct from the 404 bootstrap case above. Every writer of this path sets
    # tokens_json, so a secret that exists without it is malformed — and
    # treating it as empty would re-mint every user's token and bounce
    # omnigraph-server to recover from what may be a transient read problem.
    with pytest.raises(SyncError, match="no non-empty tokens_json"):
        read_token_map(base, "token", "secret-operations/witan/actor-tokens")


def test_read_token_map_rejects_a_non_json_success_body(stub_server):
    _, base = stub_server
    # An HTML error page from a proxy, served with a 200.
    _StubHandler.routes = {
        "/v1/secret-operations/witan/actor-tokens": (200, "<html>gateway</html>")
    }

    # Must surface as SyncError, not a bare JSONDecodeError traceback naming
    # json/decoder.py instead of the request that was misrouted.
    with pytest.raises(SyncError, match="non-JSON body"):
        read_token_map(base, "token", "secret-operations/witan/actor-tokens")


def test_read_token_map_rejects_a_non_string_map(stub_server):
    _, base = stub_server
    _StubHandler.routes = {
        "/v1/secret-operations/witan/actor-tokens": (
            200,
            {"data": {"tokens_json": json.dumps({"act-alice": {"nested": 1}})}},
        )
    }

    with pytest.raises(SyncError, match="not a"):
        read_token_map(base, "token", "secret-operations/witan/actor-tokens")


def test_realm_users_pages_to_the_end(stub_server):
    _, base = stub_server
    full_page = [member(f"u{i}") for i in range(100)]
    tail = [member("u100")]
    _StubHandler.routes = {
        "/admin/realms/r/users?first=0&max=100": (200, full_page),
        "/admin/realms/r/users?first=100&max=100": (200, tail),
    }

    # A short page is the only end-of-list signal Keycloak gives, so a realm
    # that lands exactly on the page size must still fetch the next one.
    users = realm_users(base, "r", "token")

    assert len(users) == 101
    assert users[-1]["id"] == "u100"
