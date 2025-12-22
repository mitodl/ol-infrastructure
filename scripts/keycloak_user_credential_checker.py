"""Check and audit Keycloak user credentials and identity provider connections.

This script connects to the Keycloak Admin API to find users with specific
credential and identity provider configurations. It can identify users that have
both password credentials and IDP connections, or filter by specific criteria.

Usage:
    python scripts/keycloak_user_credential_checker.py --password <admin-password>
    python scripts/keycloak_user_credential_checker.py \
        --password <admin-password> \
        --email-domain example.com \
        --has-password \
        --has-idp

Arguments:
    --password (str): The password for the Keycloak admin user (required).
    --keycloak-url (str): The base URL of the Keycloak instance.
                          Default: https://sso-qa.ol.mit.edu
    --realm (str): The Keycloak realm to operate within. Default: olapps
    --username (str): The username for the Keycloak admin user. Default: admin
    --auth-realm (str): The realm to authenticate against. Default: master
    --email-domain (str): Filter by email domain. Optional.
    --include-subdomains: Include users with subdomains of the email domain.
    --has-password: Filter for users with password credentials.
    --has-idp: Filter for users with identity provider connections.
    --has-both: Filter for users with both password and IDP credentials.
    --exclude-username (str): Username to exclude. Can be specified multiple times.
    --output-format (str): Output format: 'text', 'csv', or 'json'. Default: text
"""

import argparse
import csv
import json
import sys
from typing import Any

import httpx

HTTP_CONFLICT = 409
HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404


class KeycloakClient:
    """Client for interacting with Keycloak Admin API with automatic token refresh."""

    def __init__(
        self, keycloak_url: str, auth_realm: str, username: str, password: str
    ):
        """Initialize the Keycloak client.

        Args:
            keycloak_url: The base URL of the Keycloak instance.
            auth_realm: The Keycloak realm to authenticate against.
            username: The admin username.
            password: The admin password.
        """
        self.keycloak_url = keycloak_url
        self.auth_realm = auth_realm
        self.username = username
        self.password = password
        self._token: str | None = None

    def _get_token(self) -> str:
        """Authenticate with Keycloak and retrieve an admin access token.

        Returns:
            The admin access token.
        """
        token_url = (
            f"{self.keycloak_url}/realms/{self.auth_realm}"
            "/protocol/openid-connect/token"
        )
        payload = {
            "client_id": "admin-cli",
            "username": self.username,
            "password": self.password,
            "grant_type": "password",
        }
        response = httpx.post(token_url, data=payload)
        response.raise_for_status()
        return response.json()["access_token"]

    def _refresh_token(self) -> None:
        """Refresh the authentication token."""
        self._token = self._get_token()

    def _get_headers(self) -> dict[str, str]:
        """Get headers with current token, refreshing if needed.

        Returns:
            Headers dictionary with authorization token.
        """
        if self._token is None:
            self._refresh_token()
        return {"Authorization": f"Bearer {self._token}"}

    def _request(
        self, method: str, url: str, *, retry_on_401: bool = True, **kwargs: Any
    ) -> httpx.Response:
        """Make an HTTP request with automatic token refresh on 401.

        Args:
            method: HTTP method (GET, POST, etc.).
            url: The URL to request.
            retry_on_401: Whether to retry with a fresh token on 401 error.
            **kwargs: Additional arguments to pass to httpx.request.

        Returns:
            The HTTP response.
        """
        headers = kwargs.pop("headers", {})
        headers.update(self._get_headers())

        response = httpx.request(method, url, headers=headers, **kwargs)

        if response.status_code == HTTP_UNAUTHORIZED and retry_on_401:
            self._refresh_token()
            headers.update(self._get_headers())
            response = httpx.request(method, url, headers=headers, **kwargs)

        response.raise_for_status()
        return response

    def get_all_users(
        self,
        realm: str,
        email_domain: str | None = None,
        *,
        include_subdomains: bool = False,
    ) -> list[dict[str, Any]]:
        """Retrieve all users from Keycloak, optionally filtered by email domain.

        Args:
            realm: The Keycloak realm.
            email_domain: Optional email domain to filter users by.
            include_subdomains: If True, include users with subdomains.

        Returns:
            A list of user objects.
        """
        users_url = f"{self.keycloak_url}/admin/realms/{realm}/users"
        all_users = []
        first = 0
        max_results = 100

        while True:
            params = {"first": str(first), "max": str(max_results)}
            if email_domain:
                params["search"] = f"*@*{email_domain}"

            response = self._request("GET", users_url, params=params)
            users_batch = response.json()

            if not users_batch:
                break

            for user in users_batch:
                if email_domain:
                    email = user.get("email", "")
                    if include_subdomains:
                        if not email.endswith((f"@{email_domain}", f".{email_domain}")):
                            continue
                    elif not email.endswith(f"@{email_domain}"):
                        continue

                all_users.append(user)

            if len(users_batch) < max_results:
                break

            first += max_results

        return all_users

    def get_user_credentials(self, realm: str, user_id: str) -> list[dict[str, Any]]:
        """Retrieve credentials for a specific user.

        Args:
            realm: The Keycloak realm.
            user_id: The ID of the user.

        Returns:
            A list of credential objects.
        """
        credentials_url = (
            f"{self.keycloak_url}/admin/realms/{realm}/users/{user_id}/credentials"
        )
        response = self._request("GET", credentials_url)
        return response.json()

    def get_user_idp_links(self, realm: str, user_id: str) -> list[dict[str, Any]]:
        """Retrieve identity provider links for a specific user.

        Args:
            realm: The Keycloak realm.
            user_id: The ID of the user.

        Returns:
            A list of IDP link objects.
        """
        idp_url = f"{self.keycloak_url}/admin/realms/{realm}/users/{user_id}/federated-identity"
        try:
            response = self._request("GET", idp_url)
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == HTTP_NOT_FOUND:
                return []
            raise

    def has_password_credential(self, realm: str, user_id: str) -> bool:
        """Check if a user has a password credential.

        Args:
            realm: The Keycloak realm.
            user_id: The ID of the user.

        Returns:
            True if the user has a password credential, False otherwise.
        """
        try:
            credentials = self.get_user_credentials(realm, user_id)
            return any(
                cred.get("type") == "password" and cred.get("userLabel") != "Temporary"
                for cred in credentials
            )
        except httpx.HTTPStatusError:
            return False

    def has_idp_connection(self, realm: str, user_id: str) -> bool:
        """Check if a user has an identity provider connection.

        Args:
            realm: The Keycloak realm.
            user_id: The ID of the user.

        Returns:
            True if the user has at least one IDP connection, False otherwise.
        """
        try:
            idp_links = self.get_user_idp_links(realm, user_id)
            return len(idp_links) > 0
        except httpx.HTTPStatusError:
            return False

    def get_idp_names(self, realm: str, user_id: str) -> list[str]:
        """Get the names of all identity providers connected to a user.

        Args:
            realm: The Keycloak realm.
            user_id: The ID of the user.

        Returns:
            A list of IDP names (identityProvider field).
        """
        try:
            idp_links = self.get_user_idp_links(realm, user_id)
            return [link.get("identityProvider", "unknown") for link in idp_links]
        except httpx.HTTPStatusError:
            return []


def format_text_output(users_with_creds: list[dict[str, Any]]) -> None:
    """Print user credentials in text format.

    Args:
        users_with_creds: List of user records with credential information.
    """
    if not users_with_creds:
        print("No users found matching the criteria.")
        return

    print(f"Found {len(users_with_creds)} user(s) matching the criteria:\n")
    for user in users_with_creds:
        print(f"Username: {user['username']}")
        print(f"  Email: {user.get('email', 'N/A')}")
        print(f"  User ID: {user['id']}")
        print(f"  Has Password: {user['has_password']}")
        print(f"  Has IDP: {user['has_idp']}")
        if user["idp_names"]:
            print(f"  IDP Providers: {', '.join(user['idp_names'])}")
        print()


def format_csv_output(users_with_creds: list[dict[str, Any]]) -> None:
    """Print user credentials in CSV format.

    Args:
        users_with_creds: List of user records with credential information.
    """
    if not users_with_creds:
        print("No users found matching the criteria.")
        return

    writer = csv.DictWriter(
        sys.stdout,
        fieldnames=[
            "username",
            "email",
            "user_id",
            "has_password",
            "has_idp",
            "idp_providers",
        ],
    )
    writer.writeheader()

    for user in users_with_creds:
        writer.writerow(
            {
                "username": user["username"],
                "email": user.get("email", ""),
                "user_id": user["id"],
                "has_password": user["has_password"],
                "has_idp": user["has_idp"],
                "idp_providers": ",".join(user["idp_names"]),
            }
        )


def format_json_output(users_with_creds: list[dict[str, Any]]) -> None:
    """Print user credentials in JSON format.

    Args:
        users_with_creds: List of user records with credential information.
    """
    output = {
        "count": len(users_with_creds),
        "users": users_with_creds,
    }
    print(json.dumps(output, indent=2))


def main() -> None:
    """Check and report on Keycloak user credentials and IDP connections."""
    parser = argparse.ArgumentParser(
        description="Check and audit Keycloak user credentials and IDP connections."
    )
    parser.add_argument(
        "--password", required=True, help="The password for the Keycloak admin user."
    )
    parser.add_argument(
        "--keycloak-url",
        default="https://sso-qa.ol.mit.edu",
        help="The base URL of the Keycloak instance.",
    )
    parser.add_argument(
        "--realm", default="olapps", help="The Keycloak realm to operate within."
    )
    parser.add_argument(
        "--username", default="admin", help="The username for the Keycloak admin user."
    )
    parser.add_argument(
        "--auth-realm", default="master", help="The realm to authenticate against."
    )
    parser.add_argument(
        "--email-domain",
        help="Filter by email domain.",
    )
    parser.add_argument(
        "--include-subdomains",
        action="store_true",
        help="Include users with subdomains of the email domain.",
    )
    parser.add_argument(
        "--has-password",
        action="store_true",
        help="Filter for users with password credentials.",
    )
    parser.add_argument(
        "--has-idp",
        action="store_true",
        help="Filter for users with identity provider connections.",
    )
    parser.add_argument(
        "--has-both",
        action="store_true",
        help="Filter for users with both password and IDP credentials.",
    )
    parser.add_argument(
        "--exclude-username",
        action="append",
        help="Username to exclude. Can be specified multiple times.",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "csv", "json"],
        default="text",
        help="Output format for results.",
    )
    args = parser.parse_args()

    try:
        client = KeycloakClient(
            args.keycloak_url, args.auth_realm, args.username, args.password
        )

        print("Retrieving users from Keycloak...")
        users = client.get_all_users(
            args.realm,
            email_domain=args.email_domain,
            include_subdomains=args.include_subdomains,
        )
        print(f"Found {len(users)} users.")

        if not users:
            print("No users found with the specified criteria.")
            return

        print("Checking user credentials and IDP connections...")
        users_with_creds = []
        exclude_usernames = set(args.exclude_username or [])

        for user in users:
            username = user["username"]

            if username in exclude_usernames:
                print(f"  Skipping {username} (excluded)")
                continue

            has_password = client.has_password_credential(args.realm, user["id"])
            has_idp = client.has_idp_connection(args.realm, user["id"])
            idp_names = client.get_idp_names(args.realm, user["id"])

            # Apply filters
            if args.has_both:
                if not (has_password and has_idp):
                    continue
            elif args.has_password:
                if not has_password:
                    continue
            elif args.has_idp and not has_idp:
                continue

            users_with_creds.append(
                {
                    "username": username,
                    "email": user.get("email", ""),
                    "id": user["id"],
                    "has_password": has_password,
                    "has_idp": has_idp,
                    "idp_names": idp_names,
                }
            )

        # Output results
        if args.output_format == "csv":
            format_csv_output(users_with_creds)
        elif args.output_format == "json":
            format_json_output(users_with_creds)
        else:
            format_text_output(users_with_creds)

    except httpx.HTTPStatusError as e:
        print(f"An HTTP error occurred: {e.response.status_code} - {e.response.text}")
        sys.exit(1)
    except KeyError as e:
        print(f"An unexpected key error occurred: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
