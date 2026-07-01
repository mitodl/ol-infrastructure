#!/usr/bin/env python3
"""Export the full scim-for-keycloak enterprise plugin configuration.

Admin backend path (from JAR decompilation of BaseEndpoint + AdministrationBackendEndpoint):
  /realms/{realm}/scim/admin/backend/scim/v2/{resource}

Authentication: the plugin checks the token's 'azp' (authorized party) claim.
It accepts tokens from 'security-admin-console' (browser admin UI) or 'admin-cli'
when KC_SPI_REALM_RESTAPI_EXTENSION_SCIM_ACCEPT_ADMIN_CLI_LOGIN=true is set on
the Keycloak instance (equivalently additionalOptions spi-realm-restapi-extension-
scim-accept-admin-cli-login=true in the Keycloak CR).

Hostname check: KC_SPI_REALM_RESTAPI_EXTENSION_SCIM_ADMIN_URL_CHECK=no-context-path
means the incoming Host header must match KC_HOSTNAME (sso.ol.mit.edu).

Usage against the public URL (simplest — no port-forward needed):
    python export_scim_config.py \
        --url https://sso.ol.mit.edu \
        --realm olapps \
        --username admin \
        --password "$(vault kv get -field=password secret-operations/keycloak/admin)"

If connecting via port-forward to work around TLS SNI issues:
    kubectl port-forward -n keycloak svc/keycloak-production-service 18443:8443 \
        --context operations-production
    python export_scim_config.py \
        --url https://localhost:18443 \
        --insecure \
        --host-header sso.ol.mit.edu \
        --realm olapps \
        --username admin \
        --password "$(vault kv get -field=password secret-operations/keycloak/admin)"

Output is JSON written to stdout (redirect to a file to save it).
"""

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _ssl_ctx(insecure: bool) -> ssl.SSLContext | None:
    if not insecure:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def get_token(
    url: str,
    realm: str,
    client_id: str,
    insecure: bool,
    host_header: str | None,
    username: str | None = None,
    password: str | None = None,
    client_secret: str | None = None,
) -> str:
    token_url = f"{url}/realms/{realm}/protocol/openid-connect/token"
    if client_secret is not None:
        params: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    else:
        params = {
            "grant_type": "password",
            "client_id": client_id,
            "username": username or "",
            "password": password or "",
        }
    data = urllib.parse.urlencode(params).encode()
    headers: dict[str, str] = {}
    if host_header:
        headers["Host"] = host_header
    req = urllib.request.Request(  # noqa: S310
        token_url, data=data, method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(insecure)) as resp:  # noqa: S310
            return json.load(resp)["access_token"]
    except urllib.error.HTTPError as e:
        sys.exit(f"Token request failed ({e.code}): {e.read().decode()[:200]}")
    except urllib.error.URLError as e:
        sys.exit(f"Token request failed: {e.reason}")


def api_get(
    url: str,
    path: str,
    token: str,
    insecure: bool,
    host_header: str | None,
) -> tuple[int, Any]:
    headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/scim+json, application/json",
    }
    if host_header:
        headers["Host"] = host_header
    req = urllib.request.Request(f"{url}{path}", headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(insecure)) as resp:  # noqa: S310
            body = resp.read()
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body.decode()
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()
        except Exception:
            body = str(e)
        return e.code, body


def probe_paths(
    url: str,
    realm: str,
    token: str,
    resource: str,
    insecure: bool,
    host_header: str | None,
) -> dict[str, Any]:
    """Try all known path variations for a given resource name.

    Canonical path (from JAR decompilation of BaseEndpoint + AdministrationBackendEndpoint):
      BaseEndpoint          @Path("/v2")    -> ScimResourceServerEndpoint  (inbound SCIM)
      BaseEndpoint          @Path("/admin") -> AdministrationBaseEndpoint
      AdministrationBaseEndpoint @Path("/backend") -> AdministrationBackendEndpoint
      AdministrationBackendEndpoint @Path("/scim/v2/{s:.*}") -> handlers

      => /realms/{realm}/scim/admin/backend/scim/v2/{resource}
    """
    candidates = [
        f"/realms/{realm}/scim/admin/backend/scim/v2/{resource}",  # canonical
        f"/realms/{realm}/scim/admin/backend/{resource}",
        f"/realms/{realm}/scim/backend/{resource}",
        f"/realms/{realm}/scim/backend/v2/{resource}",
        f"/realms/{realm}/scim/v2/{resource}",
    ]
    for path in candidates:
        status, body = api_get(url, path, token, insecure, host_header)
        if status == 200:
            return {"path": path, "status": status, "body": body}
        if status not in (404, 405):
            # 403/401/500 are informative failures
            return {"path": path, "status": status, "body": body}
    return {"path": candidates[0], "status": 404, "body": None}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--url",
        required=True,
        help="Keycloak base URL (e.g. https://sso.ol.mit.edu). When port-forwarding "
        "instead of using the public URL, also pass --host-header so the admin "
        "backend hostname check passes.",
    )
    parser.add_argument("--realm", default="olapps")
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--client-id", default="admin-cli")
    parser.add_argument(
        "--client-secret",
        default=None,
        help="Client secret for client_credentials grant (use instead of username/password)",
    )
    parser.add_argument("--token", help="Pre-obtained bearer token")
    parser.add_argument("--auth-realm", default="master")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification (needed when port-forwarding to HTTPS)",
    )
    parser.add_argument(
        "--host-header",
        default=None,
        help="Override the Host header (e.g. sso.ol.mit.edu). The scim-for-keycloak "
        "admin backend checks the request hostname against KC_HOSTNAME; when "
        "port-forwarding you must spoof this to pass the check.",
    )
    parser.add_argument(
        "--output", default="-", help="Output file path (default: stdout)"
    )
    args = parser.parse_args()

    insecure: bool = args.insecure
    host_header: str | None = args.host_header
    url = args.url.rstrip("/")

    if args.token:
        token = args.token
    elif args.client_secret:
        token = get_token(
            url,
            args.auth_realm,
            args.client_id,
            insecure,
            host_header,
            client_secret=args.client_secret,
        )
    elif args.username and args.password:
        token = get_token(
            url,
            args.auth_realm,
            args.client_id,
            insecure,
            host_header,
            args.username,
            args.password,
        )
    else:
        parser.error("Provide --token, --client-secret, or --username+--password")

    realm = args.realm
    result: dict[str, Any] = {"realm": realm, "keycloak_url": url, "resources": {}}

    # ── Core plugin resources ────────────────────────────────────────────────
    resources_to_fetch = [
        ("RemoteScimProviderConfig", "Remote SCIM provider targets (MIT Learn, etc.)"),
        ("PendingRemoteOperations", "Failed/queued retry operations"),
        ("KeycloakServiceProviderConfig", "Plugin's own service provider config"),
        ("PluginMetadata", "Plugin version and license info"),
        ("RealmRoles", "Realm roles visible to the plugin"),
        ("KeycloakResourceTypes", "Resource types configured in plugin"),
        ("KeycloakResourceSchemas", "Schemas configured in plugin"),
        ("ScimMigration", "Migration state"),
    ]

    print("Probing scim-for-keycloak admin backend endpoints...", file=sys.stderr)

    for resource_name, description in resources_to_fetch:
        print(f"  Fetching {resource_name}...", file=sys.stderr)
        probe = probe_paths(url, realm, token, resource_name, insecure, host_header)
        result["resources"][resource_name] = {
            "description": description,
            "path_used": probe["path"],
            "http_status": probe["status"],
            "data": probe["body"],
        }
        status = probe["status"]
        data = probe["body"]
        if status == 200:
            count = ""
            if isinstance(data, dict):
                total = data.get("totalResults") or len(data.get("Resources", []))
                if total:
                    count = f" ({total} resource(s))"
            print(f"    OK{count} via {probe['path']}", file=sys.stderr)
        else:
            print(f"    {status} via {probe['path']}", file=sys.stderr)

    # ── Summary of pending operations ────────────────────────────────────────
    pending = result["resources"].get("PendingRemoteOperations", {})
    if pending.get("http_status") == 200 and isinstance(pending.get("data"), dict):
        ops = pending["data"].get("Resources", [])
        if ops:
            print(f"\nPending retry operations: {len(ops)}", file=sys.stderr)
            by_type: dict[str, int] = {}
            by_provider: dict[str, int] = {}
            for op in ops:
                op_type = op.get("operationType", "unknown")
                provider = op.get("remoteProvider", {}).get("name", "unknown")
                by_type[op_type] = by_type.get(op_type, 0) + 1
                by_provider[provider] = by_provider.get(provider, 0) + 1
            print(f"  By type: {by_type}", file=sys.stderr)
            print(f"  By provider: {by_provider}", file=sys.stderr)

    # ── Remote provider summary ───────────────────────────────────────────────
    providers = result["resources"].get("RemoteScimProviderConfig", {})
    if providers.get("http_status") == 200 and isinstance(providers.get("data"), dict):
        rp_list = providers["data"].get("Resources", [])
        print(f"\nRemote SCIM providers configured: {len(rp_list)}", file=sys.stderr)
        for rp in rp_list:
            print(
                f"  [{rp.get('id', '?')}] {rp.get('name', '?')} "
                f"baseUrl={rp.get('baseUrl', '?')} "
                f"enabled={rp.get('enabled', '?')} "
                f"retryOnFailure={rp.get('retryOnFailure', '?')} "
                f"retryIntervalInSeconds={rp.get('retryIntervalInSeconds', '?')}",
                file=sys.stderr,
            )

    # ── Write output ─────────────────────────────────────────────────────────
    output_json = json.dumps(result, indent=2, default=str)
    if args.output == "-":
        print(output_json)
    else:
        Path(args.output).write_text(output_json)
        print(f"\nWrote config to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
