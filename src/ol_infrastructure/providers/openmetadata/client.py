"""OpenMetadata REST API access shared by the OpenMetadata dynamic providers.

Bot users, their roles and their tokens can only be managed by an OpenMetadata
admin: UserResource calls ``authorizer.authorizeAdmin``, which checks only the
caller's ``isAdmin`` flag, so no policy can grant it. The providers therefore
authenticate as a Keycloak service account whose username is listed in the
server's ``initialAdmins`` (applications/open_metadata). OpenMetadata accepts
any token signed by the realm's keys, takes the principal from
``preferred_username`` when the token carries no ``email``, and creates every
``initialAdmins`` entry as an admin user on each boot.
"""

from dataclasses import dataclass
from typing import Any

import pulumi
import requests

from ol_infrastructure.providers.http import raise_for_status_with_body

REQUEST_TIMEOUT_SECONDS = 30
JSON_PATCH_CONTENT_TYPE = "application/json-patch+json"


@dataclass(frozen=True)
class OpenMetadataConnection:
    """Where the OpenMetadata API is and how to get a token for it.

    :param server_url: The API root, e.g. ``https://data.ol.mit.edu/api``.
    :param token_url: The Keycloak realm's token endpoint.
    :param client_id: A client whose service account is an OpenMetadata admin.
    :param client_secret: The client's secret.
    """

    server_url: pulumi.Input[str]
    token_url: pulumi.Input[str]
    client_id: pulumi.Input[str]
    client_secret: pulumi.Input[str]

    def to_props(self) -> dict[str, Any]:
        """Render the connection as dynamic resource inputs."""
        return {
            "server_url": self.server_url,
            "token_url": self.token_url,
            "client_id": self.client_id,
            "client_secret": pulumi.Output.secret(self.client_secret),
        }


class OpenMetadataClient:
    """A thin requests wrapper over the OpenMetadata ``/v1`` API.

    :param props: Resource properties holding the connection inputs.
    """

    def __init__(self, props: dict[str, Any]) -> None:
        token_response = requests.post(
            props["token_url"],
            data={
                "grant_type": "client_credentials",
                "client_id": props["client_id"],
                "client_secret": props["client_secret"],
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        raise_for_status_with_body(token_response)
        self._base_url = f"{props['server_url'].rstrip('/')}/v1"
        self._session = requests.Session()
        self._session.headers["Authorization"] = (
            f"Bearer {token_response.json()['access_token']}"
        )

    def get(self, path: str, **params: str) -> dict[str, Any] | None:
        """GET an entity, or None if it does not exist."""
        response = self._session.get(
            f"{self._base_url}/{path}", params=params, timeout=REQUEST_TIMEOUT_SECONDS
        )
        if response.status_code == requests.codes.not_found:
            return None
        raise_for_status_with_body(response)
        return response.json()

    def put(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """PUT a create-or-update request."""
        response = self._session.put(
            f"{self._base_url}/{path}", json=body, timeout=REQUEST_TIMEOUT_SECONDS
        )
        raise_for_status_with_body(response)
        return response.json()

    def patch(self, path: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
        """Apply a JSON Patch."""
        response = self._session.patch(
            f"{self._base_url}/{path}",
            json=operations,
            headers={"Content-Type": JSON_PATCH_CONTENT_TYPE},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        raise_for_status_with_body(response)
        return response.json()

    def delete(self, path: str, **params: str) -> None:
        """DELETE an entity; one that is already gone is not an error."""
        response = self._session.delete(
            f"{self._base_url}/{path}", params=params, timeout=REQUEST_TIMEOUT_SECONDS
        )
        if response.status_code == requests.codes.not_found:
            return
        raise_for_status_with_body(response)
