#!/usr/bin/env python3
"""Sync Keycloak StarRocks client role memberships into a Kubernetes ConfigMap.

The ConfigMap is consumed by StarRocks' file-based group provider.
Format: one line per non-empty role — "role_name:user1,user2,...".

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

    lines: list[str] = []
    for role in _GOVERNANCE_ROLES:
        users = _api_get_all(
            f"{admin_base}/clients/{client_uuid}/roles/{role}/users", headers
        )
        usernames = sorted(u["username"] for u in users if u.get("username"))
        if usernames:
            lines.append(f"{role}:{','.join(usernames)}")

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
