"""Superset metadata ingestion workflow with OAuth client-credentials auth.

Superset is configured with AUTH_TYPE = AUTH_OAUTH (Keycloak) and has no
local DB users, so the standard OM SupersetAuth (username/password via
/api/v1/security/login) cannot be used.  Instead we patch
SupersetAuthenticationProvider.get_access_token to perform an OIDC
client_credentials grant and return the resulting JWT directly.  Superset
validates incoming Bearer tokens via JWT_PUBLIC_KEY (the Keycloak realm public
key), so no additional FAB login step is needed.

The patch is applied at class level before MetadataWorkflow instantiates the
connector, so all downstream OM logic (dashboard/chart/dataset extraction and
lineage) runs unchanged.
"""

import os
from datetime import datetime, timedelta, timezone

import requests
from metadata.ingestion.source.dashboard.superset.client import (
    SupersetAuthenticationProvider,
)
from metadata.workflow.metadata import MetadataWorkflow


class _ClientCredentialsTokenCache:
    """Fetches and caches an OAuth2 client_credentials access token in memory.

    Mirrors the ClientCredentialsAuth pattern from ol-data-platform's
    superset_api.py, adapted to return a plain token string rather than
    acting as a requests.AuthBase.
    """

    def __init__(self, token_url: str, client_id: str, client_secret: str) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token: str | None = None
        self._expires_at: datetime | None = None

    def get(self) -> str:
        now = datetime.now(tz=timezone.utc)  # noqa: UP017 - ingestion-base runs Python 3.10
        if self._access_token is None or (
            self._expires_at is not None and self._expires_at <= now
        ):
            resp = requests.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "scope": "openid profile email roles",
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data["access_token"]
            # Subtract 30 s to allow for clock skew / network latency.
            self._expires_at = now + timedelta(seconds=data.get("expires_in", 300) - 30)
        return self._access_token


_token_cache = _ClientCredentialsTokenCache(
    token_url=(
        f"{os.environ['OM_SUPERSET_OIDC_REALM_URL']}/protocol/openid-connect/token"
    ),
    client_id=os.environ["OM_SUPERSET_OIDC_CLIENT_ID"],
    client_secret=os.environ["OM_SUPERSET_OIDC_CLIENT_SECRET"],
)


def _oauth_get_access_token(_self: SupersetAuthenticationProvider) -> tuple[str, int]:
    """Return a cached Keycloak JWT instead of calling /api/v1/security/login."""
    return _token_cache.get(), 0


SupersetAuthenticationProvider.get_access_token = _oauth_get_access_token

config = {
    "source": {
        "type": "superset",
        "serviceName": os.environ["OM_SERVICE_NAME"],
        "serviceConnection": {
            "config": {
                "type": "Superset",
                "hostPort": os.environ["OM_SUPERSET_URL"],
                "connection": {
                    # SupersetApiConnection — username/password/provider are
                    # required by the schema but never used; get_access_token
                    # is patched above to return the Keycloak JWT directly.
                    "username": "service-account",
                    "password": "not-used",  # pragma: allowlist secret
                    "provider": "db",
                },
            }
        },
        "sourceConfig": {"config": {"type": "DashboardMetadata"}},
    },
    "sink": {"type": "metadata-rest", "config": {}},
    "workflowConfig": {
        "openMetadataServerConfig": {
            "hostPort": os.environ["OM_SERVER_URL"],
            "authProvider": "openmetadata",
            "securityConfig": {"jwtToken": os.environ["OM_BOT_JWT_TOKEN"]},
        }
    },
}

workflow = MetadataWorkflow.create(config)
workflow.execute()
workflow.raise_from_status()
