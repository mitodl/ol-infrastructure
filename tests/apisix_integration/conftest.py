"""Run the APISIX plugins in this repo against a real APISIX.

The unit tests in ``tests/ol_infrastructure/components/services`` stub ``ngx``
and ``apisix.core``, so they prove the Lua does what we meant but not that
APISIX accepts the plugin config or that OpenResty behaves as assumed.  These
tests start the pinned APISIX image in standalone mode, feed it the route
config produced by the real component helpers, and assert over HTTP.

Skipped when Docker is unavailable.  ``APISIX_IT_IMAGE`` overrides the image.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

import pytest
import urllib3

if TYPE_CHECKING:
    from collections.abc import Generator

from ol_infrastructure.components.services.apisix import (
    oidc_error_callback_recovery_plugin,
)

# Must track the APISIX shipped by the chart pinned in bridge.lib.versions
# (APISIX_CHART 2.16.x => APISIX 3.17.x).  A mismatch here is the difference
# between testing what runs in production and testing something else -- which is
# also why the override is an explicit environment variable rather than a
# default that could drift: set APISIX_IT_IMAGE to rehearse a version bump
# against this suite before moving the chart pin.
APISIX_IMAGE = os.environ.get("APISIX_IT_IMAGE", "apache/apisix:3.17.0-debian")
CONTAINER_NAME = "ol-apisix-integration-test"
READY_TIMEOUT_SECONDS = 60

# Standalone (yaml) config provider, so no etcd is needed.  Mirrors the
# provider the ingress controller drives in the cluster
# (provider.type: apisix-standalone in infrastructure/aws/eks/apisix_official.py).
CONFIG_YAML = """
apisix:
  node_listen: 9080
  enable_admin: false
deployment:
  role: data_plane
  role_data_plane:
    config_provider: yaml
plugins:
  - serverless-pre-function
  - serverless-post-function
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    return (
        subprocess.run(
            ["docker", "info"],  # noqa: S607
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


requires_docker = pytest.mark.skipif(
    not _docker_available(),
    reason="docker is required to run APISIX integration tests",
)


@pytest.fixture(scope="session")
def apisix_routes() -> dict[str, Any]:
    """Build the standalone route document from the real plugin helpers.

    Routes point at a deliberately dead upstream: every assertion here is about
    what the gateway itself does before proxying, and a request that reaches the
    upstream is one the plugin correctly declined to intercept.
    """
    recovery = oidc_error_callback_recovery_plugin()
    dead_upstream = {"type": "roundrobin", "nodes": {"127.0.0.1:1": 1}}
    return {
        "routes": [
            {
                "id": "login-prefix",
                "uri": "/login/*",
                "upstream": dead_upstream,
                "plugins": {recovery.name: recovery.config},
            },
            {
                "id": "nested-login-prefix",
                "uri": "/learn/login/*",
                "upstream": dead_upstream,
                "plugins": {recovery.name: recovery.config},
            },
        ]
    }


@pytest.fixture(scope="session")
def apisix(tmp_path_factory, apisix_routes) -> Generator[str]:
    """Start APISIX on a free port and yield its base URL."""
    conf_dir = tmp_path_factory.mktemp("apisix-conf")
    (conf_dir / "config.yaml").write_text(CONFIG_YAML)
    # APISIX's standalone loader requires the #END terminator.  JSON is a YAML
    # subset, so dumping the document avoids depending on a YAML writer here.
    (conf_dir / "apisix.yaml").write_text(
        json.dumps(apisix_routes, indent=2) + "\n#END\n"
    )

    port = _free_port()
    subprocess.run(  # noqa: S603
        ["docker", "rm", "-f", CONTAINER_NAME],  # noqa: S607
        capture_output=True,
        check=False,
    )
    started = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "run",
            "-d",
            "--name",
            CONTAINER_NAME,
            "-p",
            f"127.0.0.1:{port}:9080",
            "-v",
            f"{conf_dir / 'config.yaml'}:/usr/local/apisix/conf/config.yaml:ro",
            "-v",
            f"{conf_dir / 'apisix.yaml'}:/usr/local/apisix/conf/apisix.yaml:ro",
            APISIX_IMAGE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if started.returncode != 0:
        pytest.fail(f"could not start APISIX: {started.stderr}")

    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_until_ready(base_url)
        yield base_url
    finally:
        logs = subprocess.run(  # noqa: S603
            ["docker", "logs", CONTAINER_NAME],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        subprocess.run(  # noqa: S603
            ["docker", "rm", "-f", CONTAINER_NAME],  # noqa: S607
            capture_output=True,
            check=False,
        )
        # Surfaced on failure: a Lua syntax error or a rejected plugin config
        # shows up in APISIX's error log, not in the HTTP response.
        if logs.stdout or logs.stderr:
            sys.stdout.write(f"\n--- APISIX logs ---\n{logs.stdout}{logs.stderr}")


def _wait_until_ready(base_url: str) -> None:
    """Poll until APISIX answers, rather than sleeping a fixed interval."""
    http = urllib3.PoolManager(retries=False)
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            http.request("GET", f"{base_url}/", timeout=1.0)
        except urllib3.exceptions.HTTPError:
            time.sleep(0.5)
        else:
            return
    pytest.fail(f"APISIX did not become ready within {READY_TIMEOUT_SECONDS}s")


@pytest.fixture
def callback(apisix):
    """Request the OIDC callback and return (status, headers)."""

    def _callback(
        path: str = "/login/.apisix/redirect",
        query: str = "",
        cookies: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str]]:
        headers = {}
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        response = urllib3.PoolManager(retries=False).request(
            "GET",
            f"{apisix}{path}{query}",
            headers=headers,
            redirect=False,
            timeout=10.0,
        )
        return response.status, dict(response.headers)

    return _callback
