"""Build the harness realm and get tokens from it the way apps do in production.

The realm mirrors the olapps clients the memory service trusts:

- ``learn-ai``, ``mit-learn`` and ``other-app`` are confidential app clients with
  standard token exchange enabled and the memory audience as an optional scope.
  ``other-app`` is not in the service's client map. Like the olapps clients they
  lack Keycloak's ``basic`` scope, so their tokens carry no ``sub`` until the
  memory audience scope's own ``sub`` mapper adds it.
- ``mit-learn``'s tokens also name ``learn-ai`` as an audience, as
  ``ol-mitlearn-client``'s do for ``ol-learn-ai-client``: learn-ai's routes log
  learners in through mit-learn's client, so learn-ai exchanges a token issued
  to mit-learn.
- ``ol-memory-client`` is the memory service; it is only ever an audience.
- ``ol-memory-admin-client`` is the platform's forget path (client credentials).
- ``catalog-indexer`` holds the general-memory grant as a default scope;
  ``learn-ai`` holds it as an optional one.
"""

import httpx

KEYCLOAK = "http://127.0.0.1:8098"
REALM = "ol-memory-harness"
TOKEN_URL = f"{KEYCLOAK}/realms/{REALM}/protocol/openid-connect/token"
JWKS_URL = f"{KEYCLOAK}/realms/{REALM}/protocol/openid-connect/certs"
AUDIENCE = "ol-memory-client"
AUDIENCE_SCOPE = "ol-memory-audience"
GENERAL_SCOPE = "ol-memory-general"
LEARNER_PASSWORD = "learner-pw"  # noqa: S105  # pragma: allowlist secret
EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105
SECRETS = {  # pragma: allowlist secret
    "learn-ai": "learn-ai-secret",
    "mit-learn": "mit-learn-secret",
    "other-app": "other-app-secret",
    "ol-memory-admin-client": "admin-secret",
    "catalog-indexer": "catalog-indexer-secret",
}
APP_CLIENTS = ("learn-ai", "mit-learn", "other-app")
ADMIN_TIMEOUT_SECONDS = 300


def admin_api() -> httpx.Client:
    """Return a client for the realm's admin API.

    :returns: An authenticated admin API client.
    :rtype: httpx.Client
    """
    token = (
        httpx.post(
            f"{KEYCLOAK}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": "admin",
                "password": "harness-admin",  # pragma: allowlist secret
            },
        )
        .raise_for_status()
        .json()["access_token"]
    )
    return httpx.Client(
        base_url=f"{KEYCLOAK}/admin/realms",
        headers={"authorization": f"Bearer {token}"},
        timeout=ADMIN_TIMEOUT_SECONDS,
    )


SUB_MAPPER: dict[str, object] = {
    "name": "sub",
    "protocol": "openid-connect",
    "protocolMapper": "oidc-sub-mapper",
    "config": {"access.token.claim": "true", "introspection.token.claim": "true"},
}


def _scope(
    api: httpx.Client,
    name: str,
    audience: str | None = None,
    mappers: tuple[dict[str, object], ...] = (),
) -> str:
    body: dict[str, object] = {
        "name": name,
        "protocol": "openid-connect",
        "attributes": {"include.in.token.scope": "true"},
        "protocolMappers": list(mappers),
    }
    if audience:
        body["protocolMappers"] = [
            *mappers,
            {
                "name": name,
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "config": {
                    "included.client.audience": audience,
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                },
            },
        ]
    api.post(f"/{REALM}/client-scopes", json=body).raise_for_status()
    return next(
        s["id"] for s in api.get(f"/{REALM}/client-scopes").json() if s["name"] == name
    )


def _client(api: httpx.Client, client_id: str, body: dict[str, object]) -> str:
    api.post(
        f"/{REALM}/clients", json={"clientId": client_id, **body}
    ).raise_for_status()
    clients = api.get(f"/{REALM}/clients", params={"clientId": client_id}).json()
    return clients[0]["id"]


def _service_client(api: httpx.Client, client_id: str) -> str:
    return _client(
        api,
        client_id,
        {
            "secret": SECRETS[client_id],
            "publicClient": False,
            "standardFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": True,
        },
    )


def setup_realm() -> None:
    """Recreate the harness realm from scratch."""
    api = admin_api()
    api.delete(f"/{REALM}")
    api.post("", json={"realm": REALM, "enabled": True}).raise_for_status()

    _client(
        api,
        AUDIENCE,
        {
            "publicClient": False,
            "standardFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": False,
        },
    )
    audience_scope = _scope(
        api, AUDIENCE_SCOPE, audience=AUDIENCE, mappers=(SUB_MAPPER,)
    )
    general_scope = _scope(api, GENERAL_SCOPE)
    learn_ai_audience_scope = _scope(api, "learn-ai-audience", audience="learn-ai")

    clients = {}
    for app in APP_CLIENTS:
        clients[app] = _client(
            api,
            app,
            {
                "secret": SECRETS[app],
                "publicClient": False,
                "standardFlowEnabled": True,
                "directAccessGrantsEnabled": True,
                "serviceAccountsEnabled": True,
                "redirectUris": [f"https://{app}.example/*"],
                "attributes": {"standard.token.exchange.enabled": "true"},
            },
        )
        api.put(
            f"/{REALM}/clients/{clients[app]}/optional-client-scopes/{audience_scope}"
        ).raise_for_status()
        basic = next(
            s["id"]
            for s in api.get(
                f"/{REALM}/clients/{clients[app]}/default-client-scopes"
            ).json()
            if s["name"] == "basic"
        )
        api.delete(
            f"/{REALM}/clients/{clients[app]}/default-client-scopes/{basic}"
        ).raise_for_status()
    api.put(
        f"/{REALM}/clients/{clients['learn-ai']}/optional-client-scopes/{general_scope}"
    ).raise_for_status()
    api.put(
        f"/{REALM}/clients/{clients['mit-learn']}/default-client-scopes/"
        f"{learn_ai_audience_scope}"
    ).raise_for_status()

    # As in QA: basic and the memory audience only, so no client_id claim
    # either; the service tells a client-credentials token by its missing sid.
    admin_client = _service_client(api, "ol-memory-admin-client")
    for scope in api.get(
        f"/{REALM}/clients/{admin_client}/default-client-scopes"
    ).json():
        if scope["name"] != "basic":
            api.delete(
                f"/{REALM}/clients/{admin_client}/default-client-scopes/{scope['id']}"
            ).raise_for_status()
    api.put(
        f"/{REALM}/clients/{admin_client}/default-client-scopes/{audience_scope}"
    ).raise_for_status()
    indexer = _service_client(api, "catalog-indexer")
    for scope in (audience_scope, general_scope):
        api.put(
            f"/{REALM}/clients/{indexer}/default-client-scopes/{scope}"
        ).raise_for_status()


def create_learner(username: str) -> str:
    """Create a Keycloak user and return its id.

    :param username: The new user's username.
    :returns: The user id, which every app sees as the token's ``sub``.
    :rtype: str
    """
    api = admin_api()
    api.post(
        f"/{REALM}/users",
        json={
            "username": username,
            "enabled": True,
            "email": f"{username}@example.org",
            "emailVerified": True,
            "firstName": username,
            "lastName": "Harness",
            "credentials": [
                {"type": "password", "value": LEARNER_PASSWORD, "temporary": False}
            ],
        },
    ).raise_for_status()
    return api.get(
        f"/{REALM}/users", params={"username": username, "exact": "true"}
    ).json()[0]["id"]


def _token(data: dict[str, str]) -> str:
    response = httpx.post(TOKEN_URL, data=data)
    if response.status_code != httpx.codes.OK:
        msg = f"{response.status_code} {response.text}"
        raise RuntimeError(msg)
    return response.json()["access_token"]


def login_token(client: str, username: str) -> str:
    """Return the learner's session token, as APISIX holds it after login.

    The password grant stands in for the browser flow.

    :param client: The client the learner logs in through.
    :param username: The learner's username.
    :returns: The access token.
    :rtype: str
    """
    return _token(
        {
            "grant_type": "password",
            "client_id": client,
            "client_secret": SECRETS[client],
            "username": username,
            "password": LEARNER_PASSWORD,
            "scope": "openid",
        }
    )


def exchange_token(client: str, subject_token: str, scope: str = AUDIENCE_SCOPE) -> str:
    """Exchange a learner token for one scoped to the memory service.

    :param client: The client performing the exchange.
    :param subject_token: The learner's session token.
    :param scope: The scopes to request.
    :returns: The exchanged access token.
    :rtype: str
    """
    return _token(
        {
            "grant_type": EXCHANGE_GRANT,
            "client_id": client,
            "client_secret": SECRETS[client],
            "subject_token": subject_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
            "requested_token_type": ACCESS_TOKEN_TYPE,
            "audience": AUDIENCE,
            "scope": scope,
        }
    )


def service_token(client: str, scope: str | None = None) -> str:
    """Return a client-credentials token for ``client``.

    :param client: The client.
    :param scope: Optional scopes to request.
    :returns: The access token.
    :rtype: str
    """
    data = {
        "grant_type": "client_credentials",
        "client_id": client,
        "client_secret": SECRETS[client],
    }
    if scope:
        data["scope"] = scope
    return _token(data)


if __name__ == "__main__":
    setup_realm()
    print(f"realm {REALM} ready")
