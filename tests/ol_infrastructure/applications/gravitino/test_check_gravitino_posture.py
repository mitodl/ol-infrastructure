"""Tests for the rendered-config half of the Gravitino posture probe.

What matters is that the fail-open shapes the chart can produce are caught: the
chart's own defaults (``simple`` authentication, an ``anonymous`` service admin),
a security key rendered twice so that the file's last line decides, a key that
turns a control off merely by being present, and an enabled CORS filter. For the
management port, only a client-certificate alert may count as a pass: a plain
HTTP listener, a TLS listener without client auth, or a server certificate the
probe cannot verify must each fail.
"""

import datetime
import socket
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ol_infrastructure.applications.gravitino.scripts.check_gravitino_posture import (
    check_client_cert_required,
    check_rendered_config,
    parse_properties,
)

EXPECTED = {
    "gravitino.authenticators": "oauth",
    "gravitino.authorization.enable": "true",
    "gravitino.authorization.serviceAdmins": "service-account-ol-gravitino-admin",
}
FORBIDDEN = ["gravitino.authorization.impl"]
MUST_NOT_BE_TRUE = ["gravitino.iceberg-rest.enableCorsFilter"]

GOOD_CONF = """
# THE CONFIGURATION FOR authorization
gravitino.authorization.enable = true
gravitino.authorization.serviceAdmins = service-account-ol-gravitino-admin
gravitino.authenticators = oauth
gravitino.server.webserver.customFilters =
"""


def _check(conf: str) -> list[str]:
    return check_rendered_config(
        parse_properties(conf), EXPECTED, FORBIDDEN, MUST_NOT_BE_TRUE
    )


def test_expected_config_passes():
    assert _check(GOOD_CONF) == []


def test_parse_keeps_duplicates_and_empty_values():
    pairs = parse_properties("a = 1\n# a = 2\na=3\nb =\n")
    assert pairs == [("a", "1"), ("a", "3"), ("b", "")]


def test_chart_defaults_fail():
    conf = GOOD_CONF.replace("= oauth", "= simple").replace(
        "service-account-ol-gravitino-admin", "anonymous"
    )
    failures = _check(conf)
    assert any("gravitino.authenticators" in failure for failure in failures)
    assert any("serviceAdmins" in failure for failure in failures)


def test_duplicate_security_key_fails_even_when_last_value_is_right():
    conf = "gravitino.authenticators = simple\n" + GOOD_CONF
    assert _check(conf) == [
        "gravitino.authenticators occurs 2 times, expected exactly once"
    ]


def test_missing_security_key_fails():
    conf = GOOD_CONF.replace("gravitino.authenticators = oauth\n", "")
    assert _check(conf) == [
        "gravitino.authenticators occurs 0 times, expected exactly once"
    ]


def test_forbidden_key_fails_whatever_its_value():
    conf = GOOD_CONF + "gravitino.authorization.impl =\n"
    assert _check(conf) == [
        "gravitino.authorization.impl is set ('') and must be absent"
    ]


def test_enabled_cors_filter_fails():
    assert _check(GOOD_CONF + "gravitino.iceberg-rest.enableCorsFilter = true\n") == [
        "gravitino.iceberg-rest.enableCorsFilter is enabled"
    ]
    assert _check(GOOD_CONF + "gravitino.iceberg-rest.enableCorsFilter = false\n") == []


def test_enabled_cors_filter_fails_whatever_its_case():
    assert _check(GOOD_CONF + "gravitino.iceberg-rest.enableCorsFilter = TRUE\n") == [
        "gravitino.iceberg-rest.enableCorsFilter is enabled"
    ]


# ─── mTLS check against real listeners ─────────────────────────────────────────


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    """Build a CA, a localhost server certificate it signs, and an unrelated CA."""
    directory = tmp_path_factory.mktemp("pki")
    now = datetime.datetime.now(datetime.UTC)

    def _name(common_name):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])

    def _ca(common_name):
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (
            x509.CertificateBuilder()
            .subject_name(_name(common_name))
            .issuer_name(_name(common_name))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=False,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        return key, cert

    ca_key, ca_cert = _ca("test-ca")
    _, other_ca_cert = _ca("unrelated-ca")
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("localhost"))
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    paths = {
        "ca": directory / "ca.crt",
        "cert": directory / "server.crt",
        "key": directory / "server.key",
        "other_ca": directory / "other-ca.crt",
    }
    paths["ca"].write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    paths["other_ca"].write_bytes(
        other_ca_cert.public_bytes(serialization.Encoding.PEM)
    )
    paths["cert"].write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    paths["key"].write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return paths


def _serve(context: ssl.SSLContext | None) -> int:
    """Answer every connection with an HTTP 200, over TLS when context is set."""
    listener = socket.create_server(("localhost", 0))

    def _loop():
        while True:
            conn, _ = listener.accept()
            try:
                if context is not None:
                    conn = context.wrap_socket(conn, server_side=True)
                conn.recv(1024)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            except (ssl.SSLError, OSError):
                pass
            finally:
                conn.close()

    threading.Thread(target=_loop, daemon=True).start()
    return listener.getsockname()[1]


def _server_context(pki, *, require_client_cert: bool) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(pki["cert"], pki["key"])
    if require_client_cert:
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(pki["ca"])
    return context


def test_mtls_listener_passes(pki):
    port = _serve(_server_context(pki, require_client_cert=True))
    assert check_client_cert_required("localhost", port, str(pki["ca"])) is None


def test_tls_listener_without_client_auth_fails(pki):
    port = _serve(_server_context(pki, require_client_cert=False))
    assert check_client_cert_required("localhost", port, str(pki["ca"])) == (
        f"localhost:{port} answered HTTP without a client certificate"
    )


def test_plain_http_listener_fails(pki):
    port = _serve(None)
    failure = check_client_cert_required("localhost", port, str(pki["ca"]))
    assert failure is not None
    assert "without a client-certificate alert" in failure


def test_unverifiable_server_certificate_fails(pki):
    port = _serve(_server_context(pki, require_client_cert=True))
    failure = check_client_cert_required("localhost", port, str(pki["other_ca"]))
    assert failure is not None
    assert "could not verify" in failure
