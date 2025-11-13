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
    --dry-run: Run in dry-run mode without making any changes.
    --keycloak-url (str): The base URL of the Keycloak instance.
                          Default: https://sso-qa.ol.mit.edu
    --realm (str): The Keycloak realm to operate within. Default: olapps
    --username (str): The username for the Keycloak admin user. Default: admin
    --auth-realm (str): The realm to authenticate against. Default: master
"""

# ruff: noqa: INP001, C901, PLR0912, PLR0915

import argparse
import sys
from typing import Any

import httpx

HTTP_CONFLICT = 409


def get_admin_token(
    keycloak_url: str,
    auth_realm: str,
    username: str,
    password: str,
) -> str:
    """Authenticate with Keycloak and retrieve an admin access token.

    Args:
        keycloak_url: The base URL of the Keycloak instance.
        auth_realm: The Keycloak realm to authenticate against.
        username: The admin username.
        password: The admin password.

    Returns:
        The admin access token.
    """
    token_url = f"{keycloak_url}/realms/{auth_realm}/protocol/openid-connect/token"
    payload = {
        "client_id": "admin-cli",
        "username": username,
        "password": password,
        "grant_type": "password",
    }
    response = httpx.post(token_url, data=payload)
    response.raise_for_status()
    return response.json()["access_token"]


def get_users_by_email_domain(
    keycloak_url: str, realm: str, token: str, email_domain: str
) -> list[dict[str, Any]]:
    """Retrieve users from Keycloak that match a given email domain.

    Args:
        keycloak_url: The base URL of the Keycloak instance.
        realm: The Keycloak realm.
        token: The admin access token.
        email_domain: The email domain to filter users by.

    Returns:
        A list of user objects.
    """
    users_url = f"{keycloak_url}/admin/realms/{realm}/users"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"search": f"*@*{email_domain}", "max": "1000"}
    response = httpx.get(users_url, headers=headers, params=params)
    response.raise_for_status()
    return response.json()


def get_organization_by_alias(
    keycloak_url: str, realm: str, token: str, org_alias: str
) -> dict[str, Any] | None:
    """Find an organization in Keycloak by its alias.

    Args:
        keycloak_url: The base URL of the Keycloak instance.
        realm: The Keycloak realm.
        token: The admin access token.
        org_alias: The alias of the organization to find.

    Returns:
        The organization object if found, otherwise None.
    """
    orgs_url = f"{keycloak_url}/admin/realms/{realm}/organizations"
    headers = {"Authorization": f"Bearer {token}"}
    response = httpx.get(orgs_url, headers=headers)
    response.raise_for_status()
    organizations = response.json()
    for org in organizations:
        if org.get("alias") == org_alias:
            return org
    return None


def get_organization_members(
    keycloak_url: str, realm: str, token: str, org_id: str
) -> list[dict[str, Any]]:
    """Retrieve the members of an organization in Keycloak.

    Args:
        keycloak_url: The base URL of the Keycloak instance.
        realm: The Keycloak realm.
        token: The admin access token.
        org_id: The ID of the organization.

    Returns:
        A list of user objects who are members of the organization.
    """
    members_url = f"{keycloak_url}/admin/realms/{realm}/organizations/{org_id}/members"
    headers = {"Authorization": f"Bearer {token}"}
    response = httpx.get(members_url, headers=headers)
    response.raise_for_status()
    return response.json()


def add_user_to_organization(
    keycloak_url: str, realm: str, token: str, user_id: str, org_id: str
) -> None:
    """Add a user to an organization in Keycloak.

    Args:
        keycloak_url: The base URL of the Keycloak instance.
        realm: The Keycloak realm.
        token: The admin access token.
        user_id: The ID of the user to add.
        org_id: The ID of the organization to add the user to.
    """
    add_to_org_url = (
        f"{keycloak_url}/admin/realms/{realm}/organizations/{org_id}/members"
    )
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    response = httpx.post(add_to_org_url, headers=headers, json=user_id)
    response.raise_for_status()


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
        "--dry-run",
        action="store_true",
        help="Run in dry-run mode without making any changes to the organization.",
    )
    args = parser.parse_args()

    try:
        token = get_admin_token(
            args.keycloak_url, args.auth_realm, args.username, args.password
        )

        if args.dry_run:
            print("*** DRY-RUN MODE: No changes will be made ***")  # noqa: T201

        print(  # noqa: T201
            f"Searching for users with email domain: {args.email_domain}"
        )
        users = get_users_by_email_domain(
            args.keycloak_url, args.realm, token, args.email_domain
        )
        if not users:
            print(  # noqa: T201
                "No users found with the specified email domain."
            )
            return

        print(f"Found {len(users)} users.")  # noqa: T201

        print(  # noqa: T201
            f"Searching for organization: {args.organization_alias}"
        )
        organization = get_organization_by_alias(
            args.keycloak_url, args.realm, token, args.organization_alias
        )
        if not organization:
            print(  # noqa: T201
                f"Error: Organization '{args.organization_alias}' not found."
            )
            sys.exit(1)

        org_id = organization["id"]
        print(  # noqa: T201
            f"Found organization '{organization['alias']}' with ID: {org_id}"
        )

        print("Getting existing organization members...")  # noqa: T201
        members = get_organization_members(args.keycloak_url, args.realm, token, org_id)
        member_ids = {member["id"] for member in members}
        print(f"Found {len(member_ids)} existing members.")  # noqa: T201

        exclude_domains = args.exclude_domain or []
        if exclude_domains:
            print(  # noqa: T201
                f"Excluding users from domains: {', '.join(exclude_domains)}"
            )

        for user in users:
            user_id = user["id"]
            user_email = user.get("email", "")

            if exclude_domains and any(
                user_email.endswith(f"@{domain}") for domain in exclude_domains
            ):
                print(  # noqa: T201
                    f"Skipping user {user['username']} ({user_email}) - "
                    "excluded domain."
                )
                continue

            if user_id in member_ids:
                print(  # noqa: T201
                    f"User {user['username']} ({user_id}) is already a member "
                    f"of {organization['alias']}."
                )
                continue

            print(  # noqa: T201
                f"Adding user {user['username']} ({user_id}) to organization "
                f"{organization['alias']}..."
            )
            if args.dry_run:
                print("  [DRY-RUN] Skipping actual API call.")  # noqa: T201
                continue

            try:
                add_user_to_organization(
                    args.keycloak_url, args.realm, token, user_id, org_id
                )
                print("Done.")  # noqa: T201
            except httpx.HTTPStatusError as e:
                print(  # noqa: T201
                    "Received an error: ", e.response.status_code, e.response.text
                )
                if e.response.status_code == HTTP_CONFLICT:
                    print(  # noqa: T201
                        f"User {user['username']} ({user_id}) is already a "
                        f"member of {organization['alias']}."
                    )
                elif "User does not exist" in e.response.text:
                    print(  # noqa: T201
                        f"Warning: User {user['username']} ({user_id}) not "
                        "found in Keycloak. Skipping."
                    )
                else:
                    raise

    except httpx.HTTPStatusError as e:
        print(  # noqa: T201
            f"An HTTP error occurred: {e.response.status_code} - {e.response.text}"
        )
        sys.exit(1)
    except KeyError as e:
        print(f"An unexpected key error occurred: {e}")  # noqa: T201
        sys.exit(1)


if __name__ == "__main__":
    main()
