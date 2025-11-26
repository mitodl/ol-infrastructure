"""Manage Keycloak users by assigning them to organizations based on email domain.

This script connects to the Keycloak Admin API to find users with a specific
email domain and adds them to a designated organization. This is useful for
automating the process of organizing users into organizations based on their
affiliation.

Usage:
    python scripts/keycloak_user_org_manager.py <email-domain> \
        <organization-alias> --password <admin-password>

Arguments:
    email-domain (str): The email domain to filter users by (e.g.,
        "example.com").
    organization-alias (str): The alias of the organization to which the
        users will be added.
    --password (str): The password for the Keycloak admin user.
    --exclude-domain (str): Email domain to exclude from being added. Can be
        specified multiple times.
    --include-subdomains: Include users with subdomains of the email domain.
    --dry-run: Run in dry-run mode without making any changes.
    --keycloak-url (str): The base URL of the Keycloak instance.
                          Default: https://sso-qa.ol.mit.edu
    --realm (str): The Keycloak realm to operate within. Default: olapps
    --username (str): The username for the Keycloak admin user. Default: admin
    --auth-realm (str): The realm to authenticate against. Default: master
"""


import argparse
import sys
from typing import Any

import httpx

HTTP_CONFLICT = 409
HTTP_UNAUTHORIZED = 401


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

    def get_users_by_email_domain(
        self, realm: str, email_domain: str, *, include_subdomains: bool = False
    ) -> list[dict[str, Any]]:
        """Retrieve users from Keycloak that match a given email domain.

        Args:
            realm: The Keycloak realm.
            email_domain: The email domain to filter users by.
            include_subdomains: If True, include users with subdomains.

        Returns:
            A list of user objects matching the domain criteria.
        """
        users_url = f"{self.keycloak_url}/admin/realms/{realm}/users"
        all_users = []
        first = 0
        max_results = 100

        while True:
            params = {
                "search": f"*@*{email_domain}",
                "first": str(first),
                "max": str(max_results),
            }
            response = self._request("GET", users_url, params=params)
            users_batch = response.json()

            if not users_batch:
                break

            for user in users_batch:
                email = user.get("email", "")
                if include_subdomains:
                    # Match both exact domain and subdomains
                    if email.endswith((f"@{email_domain}", f".{email_domain}")):
                        all_users.append(user)
                elif email.endswith(f"@{email_domain}"):
                    # Filter to exact domain matches only (exclude subdomains)
                    all_users.append(user)

            if len(users_batch) < max_results:
                break

            first += max_results

        return all_users

    def get_organization_by_alias(
        self, realm: str, org_alias: str
    ) -> dict[str, Any] | None:
        """Find an organization in Keycloak by its alias.

        Args:
            realm: The Keycloak realm.
            org_alias: The alias of the organization to find.

        Returns:
            The organization object if found, otherwise None.
        """
        orgs_url = f"{self.keycloak_url}/admin/realms/{realm}/organizations"
        response = self._request("GET", orgs_url)
        organizations = response.json()
        for org in organizations:
            if org.get("alias") == org_alias:
                return org
        return None

    def get_organization_members(self, realm: str, org_id: str) -> list[dict[str, Any]]:
        """Retrieve the members of an organization in Keycloak.

        Args:
            realm: The Keycloak realm.
            org_id: The ID of the organization.

        Returns:
            A list of user objects who are members of the organization.
        """
        members_url = (
            f"{self.keycloak_url}/admin/realms/{realm}/organizations/{org_id}/members"
        )
        all_members = []
        first = 0
        max_results = 100

        while True:
            params = {"first": str(first), "max": str(max_results)}
            response = self._request("GET", members_url, params=params)
            members_batch = response.json()

            if not members_batch:
                break

            all_members.extend(members_batch)

            if len(members_batch) < max_results:
                break

            first += max_results

        return all_members

    def add_user_to_organization(self, realm: str, user_id: str, org_id: str) -> None:
        """Add a user to an organization in Keycloak.

        Args:
            realm: The Keycloak realm.
            user_id: The ID of the user to add.
            org_id: The ID of the organization to add the user to.
        """
        add_to_org_url = (
            f"{self.keycloak_url}/admin/realms/{realm}/organizations/{org_id}/members"
        )
        self._request(
            "POST",
            add_to_org_url,
            headers={"Content-Type": "application/json"},
            json=user_id,
        )


def main() -> None:
    """Orchestrate the user to organization assignment."""
    parser = argparse.ArgumentParser(
        description="Assign Keycloak users to an organization based on email domain."
    )
    parser.add_argument("email_domain", help="The email domain to filter users by.")
    parser.add_argument(
        "organization_alias", help="The alias of the organization to assign users to."
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
        "--exclude-domain",
        action="append",
        help=(
            "Email domain to exclude from being added to the organization. "
            "Can be specified multiple times."
        ),
    )
    parser.add_argument(
        "--include-subdomains",
        action="store_true",
        help=(
            "Include users with subdomains of the email domain. "
            "Exclusions via --exclude-domain still apply."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in dry-run mode without making any changes to the organization.",
    )
    args = parser.parse_args()

    try:
        client = KeycloakClient(
            args.keycloak_url, args.auth_realm, args.username, args.password
        )

        if args.dry_run:
            print("*** DRY-RUN MODE: No changes will be made ***")

        print(
            f"Searching for users with email domain: {args.email_domain}"
            + (" (including subdomains)" if args.include_subdomains else "")
        )
        users = client.get_users_by_email_domain(
            args.realm, args.email_domain, include_subdomains=args.include_subdomains
        )
        if not users:
            print(
                "No users found with the specified email domain."
            )
            return

        print(f"Found {len(users)} users.")

        print(
            f"Searching for organization: {args.organization_alias}"
        )
        organization = client.get_organization_by_alias(
            args.realm, args.organization_alias
        )
        if not organization:
            print(
                f"Error: Organization '{args.organization_alias}' not found."
            )
            sys.exit(1)

        org_id = organization["id"]
        print(
            f"Found organization '{organization['alias']}' with ID: {org_id}"
        )

        print("Getting existing organization members...")
        members = client.get_organization_members(args.realm, org_id)
        member_ids = {member["id"] for member in members}
        print(f"Found {len(member_ids)} existing members.")

        exclude_domains = args.exclude_domain or []
        if exclude_domains:
            print(
                f"Excluding users from domains: {', '.join(exclude_domains)}"
            )

        for user in users:
            user_id = user["id"]
            user_email = user.get("email", "")

            if exclude_domains:
                # Check if email matches any excluded domain (exact or subdomain)
                excluded = False
                for domain in exclude_domains:
                    if user_email.endswith((f"@{domain}", f".{domain}")):
                        excluded = True
                        break
                if excluded:
                    print(
                        f"Skipping user {user['username']} ({user_email}) - "
                        "excluded domain."
                    )
                    continue

            if user_id in member_ids:
                print(
                    f"User {user['username']} ({user_id}) is already a member "
                    f"of {organization['alias']}."
                )
                continue

            print(
                f"Adding user {user['username']} ({user_id}) to organization "
                f"{organization['alias']}..."
            )
            if args.dry_run:
                print("  [DRY-RUN] Skipping actual API call.")
                continue

            try:
                client.add_user_to_organization(args.realm, user_id, org_id)
                print("Done.")
            except httpx.HTTPStatusError as e:
                print(
                    "Received an error: ", e.response.status_code, e.response.text
                )
                if e.response.status_code == HTTP_CONFLICT:
                    print(
                        f"User {user['username']} ({user_id}) is already a "
                        f"member of {organization['alias']}."
                    )
                elif "User does not exist" in e.response.text:
                    print(
                        f"Warning: User {user['username']} ({user_id}) not "
                        "found in Keycloak. Skipping."
                    )
                else:
                    raise

    except httpx.HTTPStatusError as e:
        print(
            f"An HTTP error occurred: {e.response.status_code} - {e.response.text}"
        )
        sys.exit(1)
    except KeyError as e:
        print(f"An unexpected key error occurred: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
