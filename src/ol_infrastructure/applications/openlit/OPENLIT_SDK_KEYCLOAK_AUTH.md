# Authenticating the OpenLIT SDK against our Keycloak-fronted instance

Our OpenLIT install (data cluster, see `__main__.py`) has no native auth, and the
OTLP receivers of its embedded OpenTelemetry collector accept anything they can
hear. We put **Keycloak OIDC in front of it at the APISIX gateway**. The gateway
exposes two route rules against the same `openlit` backend service:

| Route | Match | Backend | Behavior |
|-------|-------|---------|----------|
| `openlit-otlp-ingest` | `/v1/traces`, `/v1/metrics`, `/v1/logs` | embedded OTel collector (4318) | APISIX validates an `Authorization: Bearer …` Keycloak token, returns `401` if missing/invalid (no redirect) |
| `openlit-ui` | everything else | platform UI (3000) | interactive OIDC redirect (humans) |

Only OTLP over **HTTP/protobuf** is exposed — the collector's gRPC port (4317) is
not routed through the gateway, so do **not** set
`OTEL_EXPORTER_OTLP_PROTOCOL=grpc`.

Programmatic clients must present a **Keycloak access token** on every OTLP
export. Tokens come from the client-credentials flow against `ol-openlit-client`
(realm `ol-platform-engineering`, service account enabled; secret published to
Vault at `secret-operations/sso/openlit`). This document shows how.

## How the SDK sends telemetry (and why static headers don't work)

`openlit.init(otlp_endpoint=…, otlp_headers=…)` does not own an HTTP client.
It writes those values into `OTEL_EXPORTER_OTLP_ENDPOINT` /
`OTEL_EXPORTER_OTLP_HEADERS` and constructs **stock OpenTelemetry OTLP/HTTP
exporters** (`OTLPSpanExporter`, `OTLPMetricExporter`, `OTLPLogExporter`), one
per signal, each flushing from its own background thread.

Exporter headers are read **once at construction**. A Keycloak access token
expires in minutes, so `otlp_headers={"Authorization": "Bearer …"}` works for a
smoke test and then silently starts dropping batches with 401s. Don't ship that.

The supported extension point is one layer down, and there are two halves to it:

1. Every OTLP/HTTP exporter accepts a `session:` argument — a
   `requests.Session` it uses for **all** of its POSTs. `requests` invokes
   `session.auth` per request, so an auth object that refreshes a cached
   Keycloak token gives us dynamic credentials with no SDK fork.
2. `openlit.init()` **reuses pre-existing OTel SDK providers** for all three
   signals (it checks `get_tracer_provider()` / `get_meter_provider()` /
   `get_logger_provider()` and only builds its own exporters when none are
   configured). So an app that sets up its own providers — with authed
   sessions — before calling `openlit.init()` keeps every OpenLIT feature and
   owns the transport.

## Reusable implementation

Drop this into a small shared module (e.g. `openlit_keycloak_auth.py`) that each
instrumented application imports once at startup.

```python
"""Keycloak client-credentials auth for OpenLIT's OTLP export path.

Usage (call ONCE at process startup, BEFORE `openlit.init()`):

    from openlit_keycloak_auth import configure_openlit_keycloak_auth
    configure_openlit_keycloak_auth()

    import openlit
    openlit.init(application_name="my-app", environment="ci")

Required environment (12-factor; inject from Vault — see notes at bottom):
    OTEL_EXPORTER_OTLP_ENDPOINT    e.g. https://openlit-ci.ol.mit.edu
                                   (exporters append /v1/traces|metrics|logs)
    OTEL_SERVICE_NAME              e.g. my-app
    OPENLIT_KEYCLOAK_TOKEN_URL     https://sso-ci.ol.mit.edu/realms/ol-platform-engineering/protocol/openid-connect/token
    OPENLIT_KEYCLOAK_CLIENT_ID     ol-openlit-client
    OPENLIT_KEYCLOAK_CLIENT_SECRET (from Vault)

Notes:
* Thread-safe: one token cache guarded by a lock, shared by the three exporter
  background threads (each exporter gets its OWN requests.Session — sessions
  are not safely shareable across threads — but all sessions share one auth).
* Proactive refresh: tokens are renewed `_REFRESH_SKEW` seconds before
  `expires_in`, so exports virtually never go out with a stale token. A batch
  that does hit a 401 is dropped by the exporter (401 is non-retryable in
  OTLP), which is why the skew matters.
"""

from __future__ import annotations

import os
import threading
import time

import requests

# Renew this many seconds before the server-stated expiry to avoid edge races.
_REFRESH_SKEW_SECONDS = 30
# Bound the token request so a hung Keycloak cannot wedge exporter threads.
_TOKEN_REQUEST_TIMEOUT_SECONDS = 10


class KeycloakClientCredentialsAuth(requests.auth.AuthBase):
    """requests auth that injects a Keycloak access token via client-credentials.

    A single instance is shared across every OTLP session. The cached token is
    refreshed proactively before expiry.
    """

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        *,
        scope: str | None = None,
        verify: bool | str = True,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        # Dedicated session for the token endpoint so token requests never
        # recurse through an (authed) exporter session.
        self._token_session = requests.Session()
        self._token_session.verify = verify

        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._expires_at: float = 0.0  # monotonic-clock deadline

    # -- requests.auth.AuthBase protocol ---------------------------------
    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        request.headers["Authorization"] = f"Bearer {self._get_token()}"
        return request

    # -- token cache ------------------------------------------------------
    def _get_token(self) -> str:
        with self._lock:
            if self._access_token is None or time.monotonic() >= self._expires_at:
                self._refresh_locked()
            assert self._access_token is not None  # noqa: S101
            return self._access_token

    def _refresh_locked(self) -> None:
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        if self._scope:
            data["scope"] = self._scope

        resp = self._token_session.post(
            self._token_url, data=data, timeout=_TOKEN_REQUEST_TIMEOUT_SECONDS
        )
        resp.raise_for_status()
        payload = resp.json()

        self._access_token = payload["access_token"]
        expires_in = float(payload.get("expires_in", 60))
        self._expires_at = time.monotonic() + max(
            0.0, expires_in - _REFRESH_SKEW_SECONDS
        )


def _authed_session(auth: KeycloakClientCredentialsAuth) -> requests.Session:
    session = requests.Session()
    session.auth = auth
    return session


def configure_openlit_keycloak_auth() -> None:
    """Install OTel SDK providers whose OTLP exporters carry Keycloak auth.

    Because the providers exist before ``openlit.init()`` runs, OpenLIT reuses
    them for traces, metrics, and logs instead of building its own
    (unauthenticated) exporters.

    Idempotency: call this exactly once at startup. The OTel SDK logs and
    ignores attempts to overwrite a global provider, so a second call would
    leave the first configuration in place.
    """
    from opentelemetry import metrics, trace
    from opentelemetry import _logs
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    auth = KeycloakClientCredentialsAuth(
        token_url=os.environ["OPENLIT_KEYCLOAK_TOKEN_URL"],
        client_id=os.environ["OPENLIT_KEYCLOAK_CLIENT_ID"],
        client_secret=os.environ["OPENLIT_KEYCLOAK_CLIENT_SECRET"],
        scope=os.environ.get("OPENLIT_KEYCLOAK_SCOPE"),  # optional
    )

    # Resource attributes OpenLIT would otherwise stamp itself; it reuses our
    # providers, so we must provide them. OTEL_RESOURCE_ATTRIBUTES merges in on
    # top of this via Resource.create().
    resource = Resource.create(
        attributes={
            "service.name": os.environ.get("OTEL_SERVICE_NAME", "unknown-service"),
            "deployment.environment": os.environ.get(
                "OTEL_DEPLOYMENT_ENVIRONMENT", "default"
            ),
            "telemetry.sdk.name": "openlit",
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(session=_authed_session(auth)))
    )
    trace.set_tracer_provider(tracer_provider)

    metrics.set_meter_provider(
        MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(session=_authed_session(auth))
                )
            ],
        )
    )

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(session=_authed_session(auth)))
    )
    _logs.set_logger_provider(logger_provider)
```

## Application wiring

```python
# main.py / app startup — BEFORE openlit.init().
from openlit_keycloak_auth import configure_openlit_keycloak_auth

configure_openlit_keycloak_auth()

import openlit

openlit.init(application_name="my-app", environment="ci")
```

Ordering is the one hard constraint: OpenLIT decides whether to build its own
exporters by looking at the global providers at `init()` time. Install the
authed providers first and it reuses them for everything (traces, metrics, and
events). Do **not** also pass `otlp_endpoint=` / `otlp_headers=` to
`openlit.init()` — with pre-existing providers they are ignored at best, and a
static `Authorization` header in the environment is a foot-gun for any other
OTel component in the process.

## Alternatives

* **OTel credential provider entry point.** Recent `opentelemetry-exporter-otlp-proto-http`
  releases can load an authed `requests.Session` from a registered
  `opentelemetry_otlp_credential_provider` entry point, selected via
  `OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER`. Same effect as the
  code above even for exporters OpenLIT builds itself, but it requires
  packaging metadata in the app, so we default to the explicit provider setup.
* **Local collector / agent.** A sidecar OTel Collector (or Grafana Alloy)
  with the `oauth2client` auth extension handles the token lifecycle natively:
  the app exports unauthenticated OTLP to localhost and the collector
  authenticates upstream to the gateway. Zero app code; right answer for
  non-Python services.
* **Same-cluster shortcut.** Apps already on the data cluster can bypass the
  gateway entirely and export to
  `http://openlit.openlit.svc.cluster.local:4318` — the in-cluster path is
  unauthenticated (only NetworkPolicies constrain it), so no token wiring is
  needed at all.

## Sourcing the client secret from Vault

The Keycloak client (`ol-openlit-client`) and its secret are published by the
keycloak substructure to `secret-operations/sso/openlit` (key `client_secret`).
For a Kubernetes-deployed app, sync it with the Vault Secrets Operator (the
same pattern the OpenLIT stack itself uses for the APISIX OIDC plugin secret)
and surface it as `OPENLIT_KEYCLOAK_CLIENT_SECRET` via `envFrom`/`secretRef`.
Never bake the secret into an image or ConfigMap.

For a non-K8s / local-dev client, read it from Vault at startup (`hvac`, role +
mount from env — see the `vault-k8s-auth` skill) rather than committing it.

Token URLs per environment (realm `ol-platform-engineering`):

| Env | `OPENLIT_KEYCLOAK_TOKEN_URL` |
|-----|------------------------------|
| CI | `https://sso-ci.ol.mit.edu/realms/ol-platform-engineering/protocol/openid-connect/token` |
| QA | `https://sso-qa.ol.mit.edu/realms/ol-platform-engineering/protocol/openid-connect/token` |
| Production | `https://sso.ol.mit.edu/realms/ol-platform-engineering/protocol/openid-connect/token` |

## Quick smoke test (static token, NOT for production)

To validate the gateway route end-to-end before wiring the full flow, mint a
token by hand and POST an empty OTLP payload:

```bash
TOKEN=$(curl -s -X POST "$OPENLIT_KEYCLOAK_TOKEN_URL" \
  -d grant_type=client_credentials \
  -d client_id=ol-openlit-client \
  -d client_secret="$OPENLIT_KEYCLOAK_CLIENT_SECRET" | jq -r .access_token)

# Expect 200 with the token…
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST https://openlit-ci.ol.mit.edu/v1/traces \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/x-protobuf' --data-binary ''

# …and 401 without it.
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST https://openlit-ci.ol.mit.edu/v1/traces \
  -H 'Content-Type: application/x-protobuf' --data-binary ''
```

The token expires in minutes — it only proves the route works. Use
`configure_openlit_keycloak_auth()` for anything long-running.
