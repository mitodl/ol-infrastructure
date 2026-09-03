"""Tests for the council-health synthetic probe.

The property worth pinning is what "the probe worked" actually means: a 2xx
response is not enough on its own (a misconfigured proxy can return one), the
request has to carry the bearer token, and a genuine server error or an
unreachable server both have to come back as a non-zero exit — that exit code
is the only signal the CronJob-failure alert rules ever see.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any, ClassVar

import pytest

from ol_infrastructure.applications.omnigraph.scripts.check_council_health import (
    ProbeError,
    main,
    run_probe,
)


class _StubHandler(BaseHTTPRequestHandler):
    """Serves whatever ``routes`` maps the path to: (status, body)."""

    routes: ClassVar[dict[str, Any]] = {}
    #: Every request this handler has served, for tests that assert on how the
    #: probe called out rather than only on what came back.
    requests: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b""
        self.__class__.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": json.loads(raw_body) if raw_body else None,
            }
        )
        status, body = self.routes.get(self.path, (404, {"error": "not found"}))
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
    _StubHandler.routes = {}
    _StubHandler.requests = []
    yield server, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_run_probe_sends_the_bearer_token_and_query_path(stub_server):
    _, base = stub_server
    _StubHandler.routes = {
        "/graphs/council/query": (200, {"rows": [{"m.slug": "x"}], "row_count": 1})
    }

    result = run_probe(base, "council", "t")  # pragma: allowlist secret

    assert result["row_count"] == 1
    [seen] = _StubHandler.requests
    assert seen["path"] == "/graphs/council/query"
    assert seen["authorization"] == "Bearer t"
    assert seen["body"]["params"] == {}
    assert "match" in seen["body"]["query"]


def test_run_probe_targets_the_graph_id_it_is_given(stub_server):
    """The acceptance test this task names: point it at a graph id that does
    not exist and confirm the probe fails for that reason.
    """
    _, base = stub_server
    _StubHandler.routes = {"/graphs/council/query": (200, {"rows": [], "row_count": 0})}

    with pytest.raises(ProbeError, match="HTTP 404"):
        run_probe(base, "does-not-exist", "t")  # pragma: allowlist secret


def test_run_probe_raises_on_a_server_error(stub_server):
    _, base = stub_server
    _StubHandler.routes = {
        "/graphs/council/query": (
            500,
            {"error": "policy denied action 'read'", "code": "denied"},
        )
    }

    with pytest.raises(ProbeError, match="policy denied action"):
        run_probe(base, "council", "t")  # pragma: allowlist secret


def test_run_probe_rejects_a_non_json_2xx_body(stub_server):
    """A 200 with the wrong shape is a misrouted request, not a working probe —
    e.g. an HTML page from a proxy in front of the real server.
    """
    _, base = stub_server
    _StubHandler.routes = {"/graphs/council/query": (200, "<html>not json</html>")}

    with pytest.raises(ProbeError, match="non-JSON body"):
        run_probe(base, "council", "t")  # pragma: allowlist secret


def test_run_probe_rejects_a_2xx_body_missing_rows(stub_server):
    """A 200 whose body is JSON but not a query response — same failure mode
    as the non-JSON case, one layer further in.
    """
    _, base = stub_server
    _StubHandler.routes = {"/graphs/council/query": (200, {"ok": True})}

    with pytest.raises(ProbeError, match="unexpected body shape"):
        run_probe(base, "council", "t")  # pragma: allowlist secret


def test_run_probe_raises_when_the_server_is_unreachable():
    with pytest.raises(ProbeError, match="failed"):
        run_probe(
            "http://127.0.0.1:1",  # nothing listens on port 1
            "council",
            "t",  # pragma: allowlist secret
        )


def test_main_fails_closed_on_missing_env(monkeypatch):
    monkeypatch.delenv("OMNIGRAPH_SERVER_ADDR", raising=False)
    monkeypatch.delenv("OMNIGRAPH_BEARER_TOKEN", raising=False)

    assert main() == 1


def test_main_succeeds_end_to_end(stub_server, monkeypatch):
    _, base = stub_server
    _StubHandler.routes = {
        "/graphs/council/query": (200, {"rows": [{"m.slug": "x"}], "row_count": 1})
    }
    monkeypatch.setenv("OMNIGRAPH_SERVER_ADDR", base)
    monkeypatch.setenv("OMNIGRAPH_BEARER_TOKEN", "t")  # pragma: allowlist secret
    monkeypatch.delenv("OMNIGRAPH_GRAPH_ID", raising=False)

    assert main() == 0
    [seen] = _StubHandler.requests
    assert seen["path"] == "/graphs/council/query"


def test_main_fails_on_a_probe_failure(stub_server, monkeypatch):
    _, base = stub_server
    _StubHandler.routes = {"/graphs/council/query": (500, {"error": "boom"})}
    monkeypatch.setenv("OMNIGRAPH_SERVER_ADDR", base)
    monkeypatch.setenv("OMNIGRAPH_BEARER_TOKEN", "t")  # pragma: allowlist secret

    assert main() == 1
