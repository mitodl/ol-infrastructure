# Authenticating the Opik SDK against our Keycloak-fronted instance

Our Opik install (data-CI cluster, see `__main__.py`) has no native auth. We put
**Keycloak OIDC in front of it at the APISIX gateway**. The gateway exposes three
route rules for the same `opik-frontend` backend:

| Route | Match | Behavior |
|-------|-------|----------|
| `opik-api-bearer` | `/api/*` **with** `Authorization: Bearer …` | APISIX validates the JWT, returns `401` if invalid (no redirect) |
| `opik-api-session` | `/api/*` **without** a bearer header | cookie/session auth (browser SPA) |
| `opik-ui` | everything else | interactive OIDC redirect (humans) |

Programmatic SDK clients must therefore present a **Keycloak access token** in the
`Authorization: Bearer …` header on every request. This document shows how.

## How the SDK header works

The Opik Python SDK sends exactly one auth header: `Authorization`, set to the
`api_key` value **verbatim** (no `Bearer` prefix is added by the SDK). It builds a
single `httpx.Client` and reuses it for *all* traffic — tracing, REST, and file
uploads — so anything we attach to that client covers every request.

The SDK exposes a supported, public extension point for exactly this:

```python
opik.hooks.add_httpx_client_hook(
    HttpxClientHook(client_init_arguments={"auth": <httpx.Auth>})
)
```

`client_init_arguments` is splatted into `httpx.Client(**kwargs)`, and `httpx.Client`
accepts an `auth=` argument. So we register a custom `httpx.Auth` that fetches and
refreshes a Keycloak token per request. No SDK fork or patch required.

> **Do NOT also set `OPIK_API_KEY`.** If it is set, the SDK stamps a static
> `Authorization` header that would conflict with our auth flow. Leave it unset and
> let the `httpx.Auth` own the header.

## Reusable implementation

Drop this into a small shared module (e.g. `opik_keycloak_auth.py`) that each
instrumented application imports once at startup.

```python
"""Keycloak client-credentials auth for the Opik SDK.

Usage (call ONCE at process startup, BEFORE the first `opik.Opik()` / decorator):

    from opik_keycloak_auth import configure_opik_keycloak_auth
    configure_opik_keycloak_auth()

Required environment (12-factor; inject from Vault — see notes at bottom):
    OPIK_URL_OVERRIDE          e.g. https://opik-ci.ol.mit.edu/api/
    OPIK_WORKSPACE             "default" for our OSS install
    OPIK_KEYCLOAK_TOKEN_URL    https://sso.ol.mit.edu/realms/olapps/protocol/openid-connect/token
    OPIK_KEYCLOAK_CLIENT_ID    ol-opik-client
    OPIK_KEYCLOAK_CLIENT_SECRET  (from Vault)

Notes:
* Thread-safe: the token cache is guarded by a lock; concurrent SDK background
  workers share one token.
* Self-healing: on a 401 the flow fetches a fresh token once and retries the
  request, so a token that expires mid-flight does not surface as an error.
* Proactive refresh: tokens are renewed `_REFRESH_SKEW` seconds before `expires_in`
  so we almost never pay the 401-retry round trip.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Generator, Optional

import httpx

import opik
from opik.hooks import HttpxClientHook, add_httpx_client_hook

# Renew this many seconds before the server-stated expiry to avoid edge races.
_REFRESH_SKEW_SECONDS = 30
# Bound the token request so a hung Keycloak cannot wedge SDK background workers.
_TOKEN_REQUEST_TIMEOUT_SECONDS = 10


class KeycloakClientCredentialsAuth(httpx.Auth):
    """httpx auth flow that injects a Keycloak access token via client-credentials.

    A single instance is shared across every request the SDK makes. The cached
    token is refreshed proactively before expiry and reactively on a 401.
    """

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        *,
        scope: Optional[str] = None,
        verify: bool | str = True,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        # Dedicated client for the token endpoint so we never recurse through the
        # SDK's own (authed) client. `verify` mirrors the SDK's TLS setting.
        self._token_client = httpx.Client(
            timeout=_TOKEN_REQUEST_TIMEOUT_SECONDS, verify=verify
        )

        self._lock = threading.Lock()
        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0  # monotonic-clock deadline

    # -- httpx.Auth protocol --------------------------------------------------
    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._get_token()}"
        response = yield request

        if response.status_code == httpx.codes.UNAUTHORIZED:
            # Token may have been revoked or expired between fetch and use.
            # Force a refresh and retry exactly once.
            request.headers["Authorization"] = (
                f"Bearer {self._get_token(force_refresh=True)}"
            )
            yield request

    # -- token cache ----------------------------------------------------------
    def _get_token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            now = time.monotonic()
            if (
                force_refresh
                or self._access_token is None
                or now >= self._expires_at
            ):
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

        resp = self._token_client.post(self._token_url, data=data)
        resp.raise_for_status()
        payload = resp.json()

        self._access_token = payload["access_token"]
        expires_in = float(payload.get("expires_in", 60))
        self._expires_at = (
            time.monotonic() + max(0.0, expires_in - _REFRESH_SKEW_SECONDS)
        )


def configure_opik_keycloak_auth() -> None:
    """Register the Keycloak auth flow on the Opik SDK's httpx client.

    Idempotency: call this exactly once at startup. Registering the hook twice
    would stack two `auth=` overrides (last one wins) — harmless but pointless.
    """
    token_url = os.environ["OPIK_KEYCLOAK_TOKEN_URL"]
    client_id = os.environ["OPIK_KEYCLOAK_CLIENT_ID"]
    client_secret = os.environ["OPIK_KEYCLOAK_CLIENT_SECRET"]
    scope = os.environ.get("OPIK_KEYCLOAK_SCOPE")  # optional

    # Honor the same TLS verification setting the SDK uses.
    verify: bool | str = True
    if "SSL_CERT_FILE" in os.environ:
        verify = os.environ["SSL_CERT_FILE"]

    auth = KeycloakClientCredentialsAuth(
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        scope=scope,
        verify=verify,
    )

    add_httpx_client_hook(
        HttpxClientHook(
            client_modifier=None,
            client_init_arguments={"auth": auth},
        )
    )
```

## Application wiring

```python
# main.py / app startup — BEFORE any Opik client or @opik.track decorator runs.
from opik_keycloak_auth import configure_opik_keycloak_auth

configure_opik_keycloak_auth()

import opik

client = opik.Opik()  # picks up OPIK_URL_OVERRIDE / OPIK_WORKSPACE from env
```

Ordering is the one hard constraint: `add_httpx_client_hook` mutates a
process-global list that the SDK reads when it lazily constructs its `httpx.Client`.
Register the hook before the first SDK call (the first trace, the first
`opik.Opik()`, or the first `@opik.track`-decorated function), and it applies to
everything thereafter.

## Sourcing the client secret from Vault

The Keycloak client (`ol-opik-client`) and its secret are published by the keycloak
substructure. For a Kubernetes-deployed app, sync the secret with the Vault Secrets
Operator (the same pattern the Opik stack already uses for the OIDC plugin secret at
`secret-operations/sso/opik`) and surface it to the app as `OPIK_KEYCLOAK_CLIENT_SECRET`
via `envFrom`/`secretRef`. Never bake the secret into an image or ConfigMap.

For a non-K8s / local-dev client, read it from Vault at startup (`hvac`, role + mount
from env — see the `vault-k8s-auth` skill) rather than committing it.

## Quick smoke test (static token, NOT for production)

To validate the gateway route end-to-end before wiring the full flow, grab a token by
hand and set it verbatim — the SDK passes `OPIK_API_KEY` straight into the header:

```bash
TOKEN=$(curl -s -X POST "$OPIK_KEYCLOAK_TOKEN_URL" \
  -d grant_type=client_credentials \
  -d client_id=ol-opik-client \
  -d client_secret="$OPIK_KEYCLOAK_CLIENT_SECRET" | jq -r .access_token)

export OPIK_API_KEY="Bearer $TOKEN"          # SDK sends it verbatim -> hits the bearer route  # pragma: allowlist secret
export OPIK_URL_OVERRIDE="https://opik-ci.ol.mit.edu/api/"
export OPIK_WORKSPACE="default"
```

This token expires in minutes — it only proves the route works. Use
`configure_opik_keycloak_auth()` for anything long-running.
```
