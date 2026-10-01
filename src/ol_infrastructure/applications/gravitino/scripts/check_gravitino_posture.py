"""Assert that the running Gravitino deployment is authenticated and locked down.

Gravitino fails open: ``gravitino.authenticators`` defaults to ``simple`` (an
unvalidated HTTP Basic header), ``serviceAdmins`` defaults to ``anonymous`` in the
Docker image, and a dropped ``enableClientAuth`` leaves the management API open to
every pod in the cluster. Each of those yields a healthy, Ready pod, so nothing
else in the deployment would notice. This probe is the thing that notices.

It holds no client certificate and no token, so every check is made from the
position of an arbitrary pod in the cluster:

1. An unauthenticated ``GET /iceberg/v1/config`` on the Iceberg REST port is 401.
2. The management port refuses a client that presents no certificate.
3. Each security key in the rendered ``gravitino.conf`` occurs exactly once,
   with the expected value. The chart renders some keys itself, so a duplicate
   pushed through ``additionalConfigItems`` would make the file's last line win.
4. Keys whose mere presence turns a control off are absent, and neither CORS
   filter is enabled.

Stdlib only: it runs on a stock ``python:*-slim`` image from a ConfigMap. Exits
non-zero on any failure, which is the alert signal (``WorkloadJobFailed*``).
"""

import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

HTTP_UNAUTHORIZED = 401
REQUEST_TIMEOUT_SECONDS = 10
K8S_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def _tls_context(ca_file: str) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def check_unauthenticated_rejected(host: str, port: int, ca_file: str) -> str | None:
    """Return a failure message, or None when the anonymous request got a 401."""
    url = f"https://{host}:{port}/iceberg/v1/config"
    try:
        with urllib.request.urlopen(
            url, timeout=REQUEST_TIMEOUT_SECONDS, context=_tls_context(ca_file)
        ) as response:
            return f"GET {url} without a token returned {response.status}, not 401"
    except urllib.error.HTTPError as err:
        if err.code == HTTP_UNAUTHORIZED:
            return None
        return f"GET {url} without a token returned {err.code}, not 401"
    except OSError as err:
        return f"GET {url} failed before any HTTP status: {err}"


def check_client_cert_required(host: str, port: int, ca_file: str) -> str | None:
    """Return a failure message, or None when a certificate-less client is refused.

    Under TLS 1.3 the server's demand for a client certificate is only acted on
    after the client's first flight, so a "successful" handshake proves nothing.
    Send a request and treat any HTTP response as the failure.
    """
    context = _tls_context(ca_file)
    try:
        raw = socket.create_connection((host, port), timeout=REQUEST_TIMEOUT_SECONDS)
    except OSError as err:
        # Refused or unreachable proves nothing about mTLS: the listener may have
        # moved, or HTTPS may be off and the API served elsewhere in plain HTTP.
        return f"could not connect to {host}:{port} to test mTLS: {err}"
    try:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            tls.sendall(
                f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
            )
            reply = tls.recv(64)
    except ssl.SSLCertVerificationError as err:
        # Our side rejecting the server is not the server rejecting us.
        return f"could not verify {host}:{port}'s certificate to test mTLS: {err}"
    except (ssl.SSLError, ConnectionResetError, BrokenPipeError):
        return None
    finally:
        raw.close()
    if reply.startswith(b"HTTP/"):
        return f"{host}:{port} answered HTTP without a client certificate"
    return None


def read_rendered_config(namespace: str, config_map: str, key: str) -> str:
    """Fetch the rendered config file from the chart's ConfigMap."""
    api = (
        f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:"
        f"{os.environ['KUBERNETES_SERVICE_PORT']}"
        f"/api/v1/namespaces/{namespace}/configmaps/{config_map}"
    )
    token = (K8S_SA_DIR / "token").read_text().strip()
    request = urllib.request.Request(api, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(  # noqa: S310
        request,
        timeout=REQUEST_TIMEOUT_SECONDS,
        context=_tls_context(str(K8S_SA_DIR / "ca.crt")),
    ) as response:
        return json.load(response)["data"][key]


def parse_properties(text: str) -> list[tuple[str, str]]:
    """Return (key, value) for every assignment line, duplicates included."""
    pairs = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "!")) or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        pairs.append((key.strip(), value.strip()))
    return pairs


def check_rendered_config(
    pairs: list[tuple[str, str]],
    expected: dict[str, str],
    forbidden: list[str],
    must_not_be_true: list[str],
) -> list[str]:
    """Return a failure message for every security key out of line."""
    failures = []
    counts = Counter(key for key, _ in pairs)
    values = dict(pairs)
    for key, want in expected.items():
        if counts[key] != 1:
            failures.append(f"{key} occurs {counts[key]} times, expected exactly once")
        elif values[key] != want:
            failures.append(f"{key} = {values[key]!r}, expected {want!r}")
    failures.extend(
        f"{key} is set ({values[key]!r}) and must be absent"
        for key in forbidden
        if key in counts
    )
    failures.extend(
        f"{key} is enabled" for key in must_not_be_true if values.get(key) == "true"
    )
    return failures


def main() -> int:
    """Run every check and report all failures, not only the first."""
    host = os.environ["GRAVITINO_HOST"]
    ca_file = os.environ["GRAVITINO_CA_FILE"]
    failures = [
        failure
        for failure in (
            check_unauthenticated_rejected(
                host, int(os.environ["ICEBERG_REST_PORT"]), ca_file
            ),
            check_client_cert_required(
                host, int(os.environ["MANAGEMENT_PORT"]), ca_file
            ),
        )
        if failure
    ]
    rendered = read_rendered_config(
        os.environ["GRAVITINO_NAMESPACE"],
        os.environ["GRAVITINO_CONFIG_MAP"],
        os.environ["GRAVITINO_CONFIG_KEY"],
    )
    failures.extend(
        check_rendered_config(
            parse_properties(rendered),
            expected=json.loads(os.environ["EXPECTED_SETTINGS"]),
            forbidden=json.loads(os.environ["FORBIDDEN_SETTINGS"]),
            must_not_be_true=json.loads(os.environ["MUST_NOT_BE_TRUE_SETTINGS"]),
        )
    )
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)  # noqa: T201
    if failures:
        return 1
    print("OK: Gravitino posture checks passed")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
