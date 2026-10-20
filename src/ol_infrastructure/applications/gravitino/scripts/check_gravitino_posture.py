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
# How long a server gets to reject a certificate-less client before the probe
# concludes it was accepted.
ALERT_WAIT_SECONDS = 3
K8S_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
# The alerts a server sends when it requires a client certificate and gets none:
# certificate_required under TLS 1.3, handshake_failure or bad_certificate under
# TLS 1.2. Any other TLS error proves nothing about mTLS. A plain-HTTP listener,
# for one, fails with a record-layer error that must not count as a pass.
CLIENT_CERT_REJECTIONS = frozenset(
    {
        "TLSV13_ALERT_CERTIFICATE_REQUIRED",
        "SSLV3_ALERT_HANDSHAKE_FAILURE",
        "SSLV3_ALERT_BAD_CERTIFICATE",
    }
)


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


def _request_without_client_cert(host: str, port: int, ca_file: str) -> bytes:
    """Connect over TLS with no client certificate; return any HTTP reply.

    Read before writing. A TLS 1.3 server that requires a client certificate
    rejects it after the handshake and closes. Had the request already been sent,
    it would sit unread in the server's buffer, the close would go out as a RST,
    and the RST can discard the alert before it is read: the check would flap.
    Sending nothing until the server has had its chance to object avoids that.
    """
    with (
        socket.create_connection((host, port), timeout=REQUEST_TIMEOUT_SECONDS) as raw,
        _tls_context(ca_file).wrap_socket(raw, server_hostname=host) as tls,
    ):
        tls.settimeout(ALERT_WAIT_SECONDS)
        try:
            return tls.recv(64)
        except TimeoutError:
            # The server is waiting for a request, so it accepted the handshake.
            pass
        tls.settimeout(REQUEST_TIMEOUT_SECONDS)
        tls.sendall(
            f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
        )
        return tls.recv(64)


def check_client_cert_required(host: str, port: int, ca_file: str) -> str | None:
    """Return a failure message, or None when a certificate-less client is refused.

    Under TLS 1.3 the server's demand for a client certificate is only acted on
    after the client's first flight, so a "successful" handshake proves nothing.
    Pass only on one of the alerts in CLIENT_CERT_REJECTIONS.
    """
    try:
        reply = _request_without_client_cert(host, port, ca_file)
    except ssl.SSLCertVerificationError as err:
        # Our side rejecting the server is not the server rejecting us.
        return f"could not verify {host}:{port}'s certificate to test mTLS: {err}"
    except ssl.SSLError as err:
        if err.reason in CLIENT_CERT_REJECTIONS:
            return None
        return f"{host}:{port} failed TLS without a client-certificate alert: {err}"
    except OSError as err:
        # Refused, unreachable or reset proves nothing about mTLS: the listener
        # may have moved, or HTTPS may be off and the API served in plain HTTP.
        return f"could not complete a TLS exchange with {host}:{port}: {err}"
    if reply.startswith(b"HTTP/"):
        return f"{host}:{port} answered HTTP without a client certificate"
    return f"{host}:{port} sent neither HTTP nor a client-certificate alert"


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
        # Gravitino parses booleans with Boolean.parseBoolean, which ignores case.
        f"{key} is enabled"
        for key in must_not_be_true
        if values.get(key, "").lower() == "true"
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
