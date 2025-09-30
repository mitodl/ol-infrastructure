"""Starburst Galaxy API client."""

import base64
import logging
import time
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)


class StarburstAPIClient:
    """Simple HTTP client for Starburst API calls."""

    def __init__(self, domain: str, client_id: str, client_secret: str) -> None:
        self.domain = domain
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = f"https://{domain}"
        self._access_token: str | None = None
        self._token_expires_at: float | None = None
        self._session = requests.Session()

    def _is_token_expired(self) -> bool:
        """Check if the current token is expired or will expire soon."""
        if self._token_expires_at is None:
            return True
        # Add 60 second buffer to avoid using token that expires during request
        return time.time() >= (self._token_expires_at - 60)

    def _get_access_token(self) -> str:
        """Get OAuth access token for API calls."""
        # Return cached token if it's still valid
        if self._access_token and not self._is_token_expired():
            return self._access_token

        logger.debug("Requesting new access token from Starburst")

        credentials = f"{self.client_id}:{self.client_secret}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()

        try:
            response = self._session.post(
                f"{self.base_url}/oauth/v2/token",
                headers={
                    "Authorization": f"Basic {encoded_credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data="grant_type=client_credentials",
                timeout=30,
            )
            response.raise_for_status()

            token_data = response.json()
            self._access_token = token_data["access_token"]

            # Calculate expiration time
            expires_in = token_data.get("expires_in", 3600)  # Default to 1 hour
            self._token_expires_at = time.time() + expires_in

            logger.debug(
                "Successfully obtained access token, expires in %d seconds",
                expires_in,
            )

        except requests.RequestException:
            logger.exception("Failed to obtain access token")
            self._access_token = None
            self._token_expires_at = None
            raise
        else:
            return self._access_token

    def _make_authenticated_request(
        self, method: str, endpoint: str, **kwargs: Any
    ) -> requests.Response:
        """Make an authenticated request to the Starburst API."""
        # Get fresh token (will reuse if still valid)
        token = self._get_access_token()
        headers = kwargs.pop("headers", {})
        headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )

        url = f"{self.base_url}/{endpoint.lstrip('/')}"

        try:
            response = self._session.request(
                method, url, headers=headers, timeout=30, **kwargs
            )
            response.raise_for_status()

        except requests.HTTPError as e:
            # If we get 401 Unauthorized, the token might be expired
            # Clear it and retry once
            if (
                e.response is not None
                and e.response.status_code == HTTPStatus.UNAUTHORIZED
            ):
                logger.warning("Received 401, clearing cached token and retrying")
                self._access_token = None
                self._token_expires_at = None

                # Retry with fresh token
                fresh_token = self._get_access_token()
                headers["Authorization"] = f"Bearer {fresh_token}"

                response = self._session.request(
                    method, url, headers=headers, timeout=30, **kwargs
                )
                response.raise_for_status()
                return response
            else:
                raise
        else:
            return response

    def get_clusters(self) -> dict[str, Any]:
        """Fetch clusters from Starburst API."""
        response = self._make_authenticated_request("GET", "/public/api/v1/cluster")
        return response.json()

    def get_cluster_ids_by_name(self, cluster_names: list[str]) -> dict[str, str]:
        """Get cluster IDs for specific cluster names."""
        clusters_data = self.get_clusters()
        cluster_name_to_id = {}

        clusters_list = clusters_data.get("result", [])
        for cluster in clusters_list:
            cluster_id = cluster.get("clusterId")
            cluster_name = cluster.get("name")

            if cluster_name in cluster_names and cluster_id:
                cluster_name_to_id[cluster_name] = cluster_id

        return cluster_name_to_id

    def create_role(self, role_name: str, description: str = "") -> dict[str, Any]:
        """Create a new role in Starburst, or return existing role."""
        # First check if role already exists
        existing_role = self.get_role_by_name(role_name)
        if existing_role:
            logger.info(
                "Role %s already exists with ID %s",
                role_name,
                existing_role.get("roleId"),
            )
            return existing_role

        # All 3 required fields for role creation
        payload = {
            "roleName": role_name,
            "roleDescription": description,
            "grantToCreatingRole": True,
        }

        logger.info("Creating role with payload: %s", payload)

        try:
            response = self._make_authenticated_request(
                "POST", "/public/api/v1/role", json=payload
            )
            return response.json()
        except requests.HTTPError as e:
            error_detail = ""
            if e.response is not None:
                try:
                    error_detail = e.response.json()
                except ValueError:
                    error_detail = e.response.text

            # If we get a 400 error about role already existing, try to fetch it
            if (
                e.response is not None
                and e.response.status_code == HTTPStatus.BAD_REQUEST
            ):
                logger.warning(
                    "Got 400 error creating role %s, checking if it already exists: %s",
                    role_name,
                    error_detail,
                )
                existing_role = self.get_role_by_name(role_name)
                if existing_role:
                    logger.info(
                        "Role %s already exists, using existing role", role_name
                    )
                    return existing_role

            logger.exception(
                "Failed to create role. Payload was: %s, Error: %s",
                payload,
                error_detail,
            )
            raise

    def get_role_by_name(self, role_name: str) -> dict[str, Any] | None:
        """Get a role by its name."""
        try:
            # The API supports looking up by name using name=value in the URL
            # We need to encode the role name properly
            endpoint = f"/public/api/v1/role/name={quote(role_name)}"
            response = self._make_authenticated_request("GET", endpoint)
            return response.json()
        except requests.HTTPError as e:
            if (
                e.response is not None
                and e.response.status_code == HTTPStatus.NOT_FOUND
            ):
                return None
            logger.warning("Failed to get role %s: %s", role_name, e)
            return None
        except requests.RequestException as e:
            logger.warning("Failed to get role %s: %s", role_name, e)
            return None

    def grant_privilege(
        self, role_id: str, privilege: dict[str, Any]
    ) -> dict[str, Any]:
        """Grant a privilege to a role using the correct API format.

        Only handles platform-level privileges: Account, Cluster, Function.
        Catalog/Schema/Table privileges are managed by dbt.
        """
        entity_kind = privilege.get("entity_kind")
        privilege_name = privilege.get("privilege")

        # Ensure grant_option is always a boolean, never None
        grant_option = bool(privilege.get("grant_option", False))

        # Build the request payload based on API documentation
        payload = self._build_privilege_payload(
            entity_kind,
            privilege_name,
            grant_option=grant_option,
            privilege=privilege,
        )

        if "error" in payload:
            return payload

        logger.info("Sending grant payload to Starburst API: %s", payload)

        try:
            endpoint = f"/public/api/v1/role/{role_id}/privilege:grant"
            response = self._make_authenticated_request("POST", endpoint, json=payload)

            # API returns 204 No Content on success
            if response.status_code == HTTPStatus.NO_CONTENT:
                return {"status": "success", "message": "Privilege granted"}

            # Unexpected response, try to parse it
            try:
                return response.json()
            except ValueError:
                return {
                    "status": "success",
                    "status_code": response.status_code,
                    "message": "Privilege granted (non-204 response)",
                }

        except requests.HTTPError as e:
            error_detail = ""
            if e.response is not None:
                try:
                    error_detail = e.response.json()
                except ValueError:
                    error_detail = e.response.text
            logger.exception(
                "Failed to grant privilege. Payload was: %s, Error: %s",
                payload,
                error_detail,
            )
            raise
        except requests.RequestException as e:
            logger.warning(
                "Failed to grant privilege %s (%s): %s", privilege_name, entity_kind, e
            )
            raise

    def _build_privilege_payload(
        self,
        entity_kind: str | None,
        privilege_name: str | None,
        *,
        grant_option: bool,
        privilege: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the privilege payload for grant requests."""
        payload = {
            "grantKind": "Allow",
            "entityId": "",
            "entityKind": "",
            "grantOption": grant_option,
            "privilege": privilege_name,
        }

        if entity_kind == "Account":
            payload["entityKind"] = "Account"
            payload["entityId"] = "account"

        elif entity_kind == "Cluster":
            cluster_id = privilege.get("entity_id")
            if not cluster_id:
                return {"error": "Missing entity_id for cluster privilege"}
            payload["entityKind"] = "Cluster"
            payload["entityId"] = cluster_id

        elif entity_kind == "Function":
            payload["entityKind"] = "Function"
            payload["entityId"] = "*"

        else:
            # Only Account, Cluster, and Function are supported.
            # Catalog/Schema/Table privileges should be managed by dbt.
            return {
                "error": (
                    f"Unsupported entity_kind: {entity_kind}. "
                    "Only Account, Cluster, and Function are supported. "
                    "Catalog/Schema/Table privileges should be managed by dbt."
                )
            }

        return payload

    def get_role_privileges(self, role_id: str) -> list[dict[str, Any]]:
        """Get all privileges for a role."""
        try:
            response = self._make_authenticated_request(
                "GET", f"/public/api/v1/role/{role_id}"
            )
            role_data = response.json()
            return role_data.get("privileges", [])
        except requests.RequestException as e:
            logger.warning("Failed to get privileges for role %s: %s", role_id, e)
            return []

    def revoke_privilege(self, role_id: str, privilege_data: dict[str, Any]) -> bool:
        """Revoke a privilege from a role.

        Only handles platform-level privileges: Account, Cluster, Function.
        Catalog/Schema/Table privileges are managed by dbt.
        """
        entity_kind = privilege_data.get("entity_kind")
        privilege = privilege_data.get("privilege")

        # Build the request payload based on API documentation
        payload = {
            "entityId": "",
            "entityKind": "",
            "privilege": privilege,
            "revokeAction": "RemoveRoleGrant",
        }

        if entity_kind == "Account":
            payload["entityKind"] = "Account"
            payload["entityId"] = "account"

        elif entity_kind == "Cluster":
            entity_id = privilege_data.get("entity_id")
            if not entity_id:
                logger.warning("Missing entity_id for cluster privilege revoke")
                return False
            payload["entityKind"] = "Cluster"
            payload["entityId"] = entity_id

        elif entity_kind == "Function":
            payload["entityKind"] = "Function"
            payload["entityId"] = "*"

        else:
            # Only Account, Cluster, and Function are supported.
            # Catalog/Schema/Table privileges should be managed by dbt.
            logger.warning(
                "Revoke not supported for entity_kind: %s. "
                "Only Account, Cluster, and Function are supported. "
                "Catalog/Schema/Table privileges should be managed by dbt.",
                entity_kind,
            )
            return False

        logger.info("Sending revoke payload to Starburst API: %s", payload)

        try:
            endpoint = f"/public/api/v1/role/{role_id}/privilege:revoke"
            response = self._make_authenticated_request("POST", endpoint, json=payload)
        except requests.RequestException as e:
            logger.warning("Failed to revoke privilege %s: %s", privilege, e)
            return False
        else:
            return response.status_code in [HTTPStatus.OK, HTTPStatus.NO_CONTENT]

    def update_role_description(self, role_id: str, description: str) -> bool:
        """Update a role's description."""
        try:
            payload = {"roleDescription": description}
            response = self._make_authenticated_request(
                "PATCH", f"/public/api/v1/role/{role_id}", json=payload
            )
        except requests.RequestException as e:
            logger.warning("Failed to update role description for %s: %s", role_id, e)
            return False
        else:
            return response.status_code == HTTPStatus.OK

    def delete_role(self, role_id: str) -> bool:
        """Delete a role."""
        try:
            self._make_authenticated_request("DELETE", f"/public/api/v1/role/{role_id}")
        except requests.RequestException as e:
            logger.exception("Failed to delete role %s", role_id)

            if hasattr(e, "response") and e.response is not None:
                status_code = e.response.status_code

                if status_code == HTTPStatus.NOT_FOUND:
                    logger.warning(
                        "Role %s not found during deletion "
                        "(may have already been deleted)",
                        role_id,
                    )
                    return False

                elif (
                    HTTPStatus.BAD_REQUEST
                    <= status_code
                    < HTTPStatus.INTERNAL_SERVER_ERROR
                ):
                    logger.exception(
                        "Client error deleting role %s: HTTP %d", role_id, status_code
                    )
                    raise

                elif status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
                    logger.exception(
                        "Server error deleting role %s: HTTP %d", role_id, status_code
                    )
                    return False

            logger.exception(
                "Network or request error deleting role %s: %s",
                role_id,
                type(e).__name__,
            )
            return False
        else:
            logger.info("Successfully deleted role %s", role_id)
            return True
