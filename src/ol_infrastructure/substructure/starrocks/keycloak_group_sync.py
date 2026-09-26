#!/usr/bin/env python3
"""Sync Keycloak StarRocks client role memberships into a Kubernetes ConfigMap.

The ConfigMap is consumed by StarRocks' file-based group provider.
Format: one line per non-empty role — "role_name:user1,user2,...", where each
user is the holder's saml_uid (the starrocks_username claim).

Required environment variables:
  KEYCLOAK_ISSUER_URL    - Keycloak realm issuer URL
                           e.g. https://sso.ol.mit.edu/realms/ol-data-platform
  KEYCLOAK_CLIENT_ID     - OAuth2 client ID (ol-starrocks-client)
  KEYCLOAK_CLIENT_SECRET - OAuth2 client secret

The ol-starrocks-client service account holds view-realm and view-users on the
realm-management client (see substructure/keycloak/ol_data_platform.py), so
its client credentials are sufficient to enumerate role memberships.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_GOVERNANCE_ROLES: tuple[str, ...] = (
    "ol_business_analyst",
    "ol_data_analyst",
    "ol_data_engineer",
    "ol_instructor",
    "ol_platform_admin",
    "ol_researcher",
)


_PAGE_SIZE = 100
# groups.txt is "role:user1,user2" per line, so a principal carrying ":", "," or
# a newline would corrupt the file or smuggle in a group entry.
_PRINCIPAL_PATTERN = re.compile(r"[A-Za-z0-9._-]+")


def _api_get(url: str, headers: dict[str, str]) -> Any:
    """GET url and return the parsed JSON body; exit on HTTP error."""
    req = urllib.request.Request(url, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        sys.exit(f"Keycloak API error {exc.code} for {url}: {exc.read().decode()}")


def _api_get_all(url: str, headers: dict[str, str]) -> list[Any]:
    """GET a paginated Keycloak collection, following first/max until exhausted.

    The Keycloak Admin API caps role-membership endpoints at max=100 by default,
    so a role with more than 100 members would otherwise be silently truncated.
    """
    results: list[Any] = []
    first = 0
    sep = "&" if "?" in url else "?"
    while True:
        page = _api_get(f"{url}{sep}first={first}&max={_PAGE_SIZE}", headers)
        if not page:
            break
        results.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        first += _PAGE_SIZE
    return results


def effective_role_members(
    admin_base: str, client_uuid: str, headers: dict[str, str]
) -> dict[str, set[str]]:
    """Map each governance role to the saml_uid of every user who holds it.

    GET /clients/{id}/roles/{role}/users returns direct mappings only, and the
    realm hands these roles out through composite realm roles (ol-starrocks-*),
    so that endpoint misses nearly everyone. Reading each user's effective
    client role mappings covers composites and group membership.

    The value written is saml_uid because both security integrations set
    principal_field=starrocks_username, which Keycloak fills from saml_uid.
    Writing any other form (e.g. the Keycloak username, an email) makes the
    group provider return no groups, and permitted_groups then refuses login.

    :param admin_base: Keycloak admin API base URL for the realm.
    :param client_uuid: Internal id of the ol-starrocks-client client.
    :param headers: Authorization headers for the admin API.
    :returns: Governance role name to the set of saml_uid values holding it.
    :rtype: dict[str, set[str]]
    """
    members: dict[str, set[str]] = {role: set() for role in _GOVERNANCE_ROLES}
    holders = 0
    for user in _api_get_all(f"{admin_base}/users?briefRepresentation=false", headers):
        if not user["enabled"]:
            continue
        mapped = _api_get(
            f"{admin_base}/users/{user['id']}/role-mappings/clients/"
            f"{client_uuid}/composite",
            headers,
        )
        granted = {role["name"] for role in mapped} & members.keys()
        if not granted:
            continue
        holders += 1
        saml_uid = user.get("attributes", {}).get("saml_uid", [""])[0]
        if not _PRINCIPAL_PATTERN.fullmatch(saml_uid):
            sys.stderr.write(
                f"Skipping {user['username']}: holds {sorted(granted)} but saml_uid "
                f"{saml_uid!r} is missing or not a valid StarRocks principal\n"
            )
            continue
        for role in granted:
            members[role].add(saml_uid)
    # The admin API omits attributes the realm's user profile doesn't expose, while
    # protocol mappers still read them. If every holder lacks saml_uid, that is the
    # likely cause, and writing an empty file would silently deny every OIDC login.
    if holders and not any(members.values()):
        sys.exit(
            f"{holders} users hold governance roles but none has a usable saml_uid; "
            "check the realm's unmanagedAttributePolicy before writing an empty file"
        )
    return members


def main() -> None:
    """Fetch Keycloak role memberships and apply them to a Kubernetes ConfigMap."""
    parser = argparse.ArgumentParser(
        description="Sync Keycloak role memberships to a StarRocks group file ConfigMap"
    )
    parser.add_argument("--namespace", default="starrocks")
    parser.add_argument("--configmap", required=True, help="ConfigMap name to write")
    args = parser.parse_args()

    issuer = os.environ["KEYCLOAK_ISSUER_URL"].rstrip("/")
    client_id = os.environ["KEYCLOAK_CLIENT_ID"]
    client_secret = os.environ["KEYCLOAK_CLIENT_SECRET"]

    base_url, _, realm_path = issuer.partition("/realms/")
    realm = realm_path.rstrip("/")
    admin_base = f"{base_url}/admin/realms/{realm}"

    token_body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode()
    try:
        with urllib.request.urlopen(  # noqa: S310
            urllib.request.Request(  # noqa: S310
                f"{issuer}/protocol/openid-connect/token",
                data=token_body,
                method="POST",
            )
        ) as resp:
            access_token = json.loads(resp.read())["access_token"]
    except urllib.error.HTTPError as exc:
        sys.exit(f"Token request failed: {exc.code} {exc.read().decode()}")

    headers = {"Authorization": f"Bearer {access_token}"}

    clients = _api_get(f"{admin_base}/clients?clientId=ol-starrocks-client", headers)
    if not clients:
        sys.exit("ol-starrocks-client not found in Keycloak realm")
    client_uuid = clients[0]["id"]

    members = effective_role_members(admin_base, client_uuid, headers)
    lines = [
        f"{role}:{','.join(sorted(members[role]))}"
        for role in _GOVERNANCE_ROLES
        if members[role]
    ]

    manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": args.configmap, "namespace": args.namespace},
        "data": {"groups.txt": "\n".join(lines)},
    }
    result = subprocess.run(
        ["kubectl", "apply", "--validate=false", "-f", "-"],  # noqa: S607
        input=json.dumps(manifest).encode(),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        sys.exit(
            f"kubectl apply failed (exit {result.returncode}):\n"
            f"{result.stderr.decode()}"
        )
    sys.stdout.write(result.stdout.decode().strip() + "\n")


if __name__ == "__main__":
    main()
