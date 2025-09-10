"""Starburst Galaxy API client."""

import base64
from typing import Any

import requests


class StarburstAPIClient:
    """Simple HTTP client for Starburst API calls."""

    def __init__(self, domain: str, client_id: str, client_secret: str) -> None:
        self.domain = domain
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = f"https://{domain}"
        self._access_token: str | None = None
        self._session = requests.Session()

    def _get_access_token(self) -> str:
        """Get OAuth access token for API calls."""
        if self._access_token:
            return self._access_token

        credentials = f"{self.client_id}:{self.client_secret}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()

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
        return self._access_token

    def _make_authenticated_request(
        self, method: str, endpoint: str, **kwargs: Any
    ) -> requests.Response:
        """Make an authenticated request to the Starburst API."""
        token = self._get_access_token()
        headers = kwargs.pop("headers", {})
        headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )

        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = self._session.request(
            method, url, headers=headers, timeout=30, **kwargs
        )
        response.raise_for_status()
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
        """Create a new role in Starburst."""
        payload = {"roleName": role_name, "roleDescription": description}
        response = self._make_authenticated_request(
            "POST", "/public/api/v1/role", json=payload
        )
        return response.json()

    def grant_privilege(
        self, role_id: str, privilege: dict[str, Any]
    ) -> dict[str, Any]:
        """Grant a privilege to a role."""
        response = self._make_authenticated_request(
            "POST", f"/public/api/v1/role/{role_id}/privilege", json=privilege
        )
        return response.json()

    def delete_role(self, role_id: str) -> bool:
        """Delete a role."""
        try:
            self._make_authenticated_request("DELETE", f"/public/api/v1/role/{role_id}")
        except requests.RequestException:
            return False
        else:
            return True
