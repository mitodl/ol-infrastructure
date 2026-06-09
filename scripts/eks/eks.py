#!/usr/bin/env python3
"""Manage kubeconfig generation and exec-based auth for OL EKS clusters."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import webbrowser
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import boto3
import cyclopts
import hvac
import yaml

app = cyclopts.App(help="Manage MIT Open Learning EKS kubeconfig setup and auth.")

AWS_REGION = "us-east-1"
AWS_DEFAULT_REGION = "us-east-1"
CACHE_DIR = Path.home() / ".cache" / "ol-infrastructure" / "eks"
KUBECONFIG_DEFAULT_PATH = Path.home() / ".kube" / "config"
OIDC_CALLBACK_PORT = 8250
OIDC_REDIRECT_URI = f"http://localhost:{OIDC_CALLBACK_PORT}/oidc/callback"
PRODUCTION_VAULT_ADDRESS = "https://vault-production.odl.mit.edu"
PREFERRED_DEFAULT_CONTEXT = "applications-qa"

# Role names registered as EKS access entries that are NOT the cluster admin role.
# Used to filter access entries when discovering the admin role ARN.
SHARED_ACCESS_ENTRY_ROLE_NAMES: frozenset[str] = frozenset(
    {
        "eks-cluster-shared-readonly-role",
        "eks-cluster-shared-developer-role",
    }
)

SELF_CLOSING_PAGE = """
<!doctype html>
<html>
<head>
<script>
window.onload = function load() {
  window.open('', '_self', '');
  window.close();
};
</script>
</head>
<body>
  <p>Authentication successful, you can close the browser now.</p>
  <script>
    setTimeout(function() {
      window.close()
    }, 5000);
  </script>
</body>
</html>
"""
VAULT_AWS_ROLE_BY_MODE = {
    "readonly": "eks-cluster-shared-readonly-role",
    "developer": "eks-cluster-shared-developer-role",
}
VAULT_OIDC_ROLE_BY_MODE = {
    "readonly": "readonly",
    "developer": "developer",
    "admin": "admin",
}


class AccessMode(StrEnum):
    """Supported access modes for generated kubeconfig entries."""

    READONLY = "readonly"
    DEVELOPER = "developer"
    ADMIN = "admin"


@dataclass(slots=True)
class ClusterConfig:
    """Cluster connection details sourced from the AWS EKS API."""

    cluster_name: str
    server: str
    certificate_authority_data: str
    admin_role_arn: str


@dataclass(slots=True)
class VaultTokenCache:
    """Cached Vault token state."""

    token: str


@dataclass(slots=True)
class AwsCredentialsCache:
    """Cached AWS credentials generated from Vault."""

    access_key: str
    secret_key: str
    session_token: str
    expires_at: str


class OidcHttpServer(HTTPServer):
    """HTTP server that stores the Vault OIDC callback code."""

    token: str | None = None


class OidcCallbackHandler(BaseHTTPRequestHandler):
    """Capture the OIDC callback code and return a self-closing page."""

    def do_GET(self) -> None:
        """Handle the OIDC callback."""
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.server.token = params["code"][0]  # type: ignore[attr-defined]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(SELF_CLOSING_PAGE.encode())

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Silence default HTTP request logging."""


def json_datetime_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(tz=UTC)


def cache_file(name: str) -> Path:
    """Return the cache file path for a given cache key."""
    return CACHE_DIR / f"{name}.json"


def load_json(path: Path) -> dict[str, Any] | None:
    """Load a JSON file if it exists."""
    if not path.exists():
        return None
    return json.loads(path.read_text())


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON payloads to a cache file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def run_command(
    command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None
) -> str:
    """Run a subprocess and return stdout or raise a helpful error."""
    completed = subprocess.run(  # noqa: S603
        command,
        check=False,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        details = stderr or stdout or f"command exited with {completed.returncode}"
        msg = f"Command failed: {' '.join(command)}\n{details}"
        raise RuntimeError(msg)
    return completed.stdout


def make_eks_client(credentials: AwsCredentialsCache) -> Any:
    """Create a boto3 EKS client authenticated with Vault-generated credentials."""
    return boto3.client(
        "eks",
        region_name=AWS_REGION,
        aws_access_key_id=credentials.access_key,
        aws_secret_access_key=credentials.secret_key,
        aws_session_token=credentials.session_token,
    )


def fetch_admin_role_arn(eks_client: Any, cluster_name: str) -> str:
    """Find the cluster admin IAM role ARN via EKS access entries.

    Iterates access entries for the cluster and returns the ARN of the first entry
    that has AmazonEKSClusterAdminPolicy associated with it, skipping known shared
    roles.
    """
    paginator = eks_client.get_paginator("list_access_entries")
    for page in paginator.paginate(clusterName=cluster_name):
        for entry_arn in page["accessEntries"]:
            role_name = entry_arn.split("/")[-1]
            if role_name in SHARED_ACCESS_ENTRY_ROLE_NAMES:
                continue
            policies_response = eks_client.list_associated_access_policies(
                clusterName=cluster_name, principalArn=entry_arn
            )
            for policy in policies_response["associatedAccessPolicies"]:
                if "AmazonEKSClusterAdminPolicy" in policy["policyArn"]:
                    return entry_arn
    msg = f"No cluster admin role found in EKS access entries for cluster {cluster_name!r}"
    raise RuntimeError(msg)


def fetch_all_cluster_configs(mode: AccessMode) -> list[ClusterConfig]:
    """Discover all EKS clusters via the AWS API using Vault-generated credentials.

    Uses readonly credentials for cluster discovery regardless of operator mode —
    listing and describing clusters requires only read access to the EKS API.
    For admin mode, also discovers each cluster's admin IAM role from access entries.
    """
    # All modes use the readonly AWS role for cluster discovery.
    # Admin mode authenticates via the admin OIDC role but requests readonly
    # AWS credentials, which are sufficient for eks:ListClusters / DescribeCluster.
    discovery_mode = AccessMode.READONLY if mode is AccessMode.ADMIN else mode
    discovery_credentials = load_valid_aws_credentials(discovery_mode)
    eks_client = make_eks_client(discovery_credentials)

    paginator = eks_client.get_paginator("list_clusters")
    cluster_names: list[str] = []
    for page in paginator.paginate():
        cluster_names.extend(page["clusters"])
    cluster_names.sort()

    configs: list[ClusterConfig] = []
    for cluster_name in cluster_names:
        detail = eks_client.describe_cluster(name=cluster_name)["cluster"]
        admin_role_arn = ""
        if mode is AccessMode.ADMIN:
            admin_role_arn = fetch_admin_role_arn(eks_client, cluster_name)
        configs.append(
            ClusterConfig(
                cluster_name=cluster_name,
                server=detail["endpoint"],
                certificate_authority_data=detail["certificateAuthority"]["data"],
                admin_role_arn=admin_role_arn,
            )
        )
    return configs


def kubeconfig_exec_args(cluster: ClusterConfig, mode: AccessMode) -> list[str]:
    """Build exec args for a kubeconfig user entry."""
    args = [
        "run",
        "python",
        str(Path(__file__).resolve()),
        "exec-credential",
        "--cluster-name",
        cluster.cluster_name,
        "--mode",
        mode.value,
    ]
    if mode is AccessMode.ADMIN:
        args.extend(["--admin-role-arn", cluster.admin_role_arn])
    return args


def resolve_current_context(
    clusters: list[ClusterConfig], current_context: str | None
) -> str | None:
    """Choose an explicit or sensible default current context.

    The default context is always an operator context (not a -readonly suffixed one).
    """
    if current_context:
        return current_context
    cluster_names = [cluster.cluster_name for cluster in clusters]
    if PREFERRED_DEFAULT_CONTEXT in cluster_names:
        return PREFERRED_DEFAULT_CONTEXT
    if cluster_names:
        return cluster_names[0]
    return None


def build_kubeconfig(
    clusters: list[ClusterConfig],
    operator_mode: AccessMode,
    current_context: str | None,
) -> dict[str, Any]:
    """Build a kubeconfig covering all clusters with dual contexts per cluster.

    For operator modes (developer, admin) each cluster gets two contexts:
      - ``<cluster-name>``          — operator credentials (read/write)
      - ``<cluster-name>-readonly`` — readonly credentials (safe for agents)

    For readonly mode a single context per cluster is generated since there is
    no separate operator credential to pair it with.

    The current-context always points to the operator (non-readonly) context.
    """
    kube_clusters = []
    contexts = []
    users = []

    for cluster in clusters:
        # One cluster entry shared by both contexts
        kube_clusters.append(
            {
                "name": cluster.cluster_name,
                "cluster": {
                    "certificate-authority-data": cluster.certificate_authority_data,
                    "server": cluster.server,
                },
            }
        )

        if operator_mode is not AccessMode.READONLY:
            # Operator context — developer or admin credentials
            operator_user = f"{cluster.cluster_name}-{operator_mode.value}"
            contexts.append(
                {
                    "name": cluster.cluster_name,
                    "context": {
                        "cluster": cluster.cluster_name,
                        "user": operator_user,
                    },
                }
            )
            users.append(
                {
                    "name": operator_user,
                    "user": {
                        "exec": {
                            "apiVersion": "client.authentication.k8s.io/v1beta1",
                            "command": "uv",
                            "args": kubeconfig_exec_args(cluster, operator_mode),
                            "interactiveMode": "IfAvailable",
                            "provideClusterInfo": False,
                        },
                    },
                }
            )

            # Readonly context — always present alongside operator contexts
            readonly_context_name = f"{cluster.cluster_name}-readonly"
            readonly_user = f"{cluster.cluster_name}-readonly"
            contexts.append(
                {
                    "name": readonly_context_name,
                    "context": {
                        "cluster": cluster.cluster_name,
                        "user": readonly_user,
                    },
                }
            )
            users.append(
                {
                    "name": readonly_user,
                    "user": {
                        "exec": {
                            "apiVersion": "client.authentication.k8s.io/v1beta1",
                            "command": "uv",
                            "args": kubeconfig_exec_args(cluster, AccessMode.READONLY),
                            "interactiveMode": "IfAvailable",
                            "provideClusterInfo": False,
                        },
                    },
                }
            )
        else:
            # Readonly-only setup — single context per cluster
            contexts.append(
                {
                    "name": cluster.cluster_name,
                    "context": {
                        "cluster": cluster.cluster_name,
                        "user": cluster.cluster_name,
                    },
                }
            )
            users.append(
                {
                    "name": cluster.cluster_name,
                    "user": {
                        "exec": {
                            "apiVersion": "client.authentication.k8s.io/v1beta1",
                            "command": "uv",
                            "args": kubeconfig_exec_args(cluster, AccessMode.READONLY),
                            "interactiveMode": "IfAvailable",
                            "provideClusterInfo": False,
                        },
                    },
                }
            )

    kube_config: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Config",
        "preferences": {},
        "clusters": kube_clusters,
        "contexts": contexts,
        "users": users,
    }
    resolved_current_context = resolve_current_context(clusters, current_context)
    if resolved_current_context:
        kube_config["current-context"] = resolved_current_context
    return kube_config


def login_oidc_get_token() -> str:
    """Wait for the Vault OIDC callback and return the authorization code."""
    httpd = OidcHttpServer(("", OIDC_CALLBACK_PORT), OidcCallbackHandler)
    httpd.handle_request()
    if not httpd.token:
        msg = "Vault OIDC callback did not return an authorization code"
        raise RuntimeError(msg)
    return httpd.token


def oidc_login(client: hvac.Client, role: str) -> str:
    """Authenticate to Vault using OIDC and return the client token."""
    auth_url_response = client.auth.oidc.oidc_authorization_url_request(
        role=role,
        redirect_uri=OIDC_REDIRECT_URI,
    )
    auth_url = auth_url_response["data"]["auth_url"]
    if not auth_url:
        msg = "Unable to retrieve auth URL from Vault"
        raise RuntimeError(msg)

    params = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)
    auth_url_nonce = params["nonce"][0]
    auth_url_state = params["state"][0]

    webbrowser.open(auth_url)
    code = login_oidc_get_token()
    auth_result = client.auth.oidc.oidc_callback(
        code=code,
        nonce=auth_url_nonce,
        state=auth_url_state,
    )
    return auth_result["auth"]["client_token"]


def vault_client(token: str | None = None) -> hvac.Client:
    """Create a Vault client for the production Vault instance."""
    return hvac.Client(url=PRODUCTION_VAULT_ADDRESS, token=token)


def load_valid_vault_token(mode: AccessMode) -> str:
    """Load a cached Vault token or authenticate via OIDC."""
    oidc_role = VAULT_OIDC_ROLE_BY_MODE[mode.value]
    token_cache_path = cache_file(f"vault-token-{oidc_role}")
    cached_payload = load_json(token_cache_path)
    if cached_payload and cached_payload.get("token"):
        token = str(cached_payload["token"])
        client = vault_client(token)
        if client.is_authenticated():
            return token

    client = vault_client()
    token = oidc_login(client, oidc_role)
    dump_json(token_cache_path, asdict(VaultTokenCache(token=token)))
    return token


def parse_expiration(value: str) -> datetime:
    """Parse an ISO8601 timestamp from cache data."""
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def load_valid_aws_credentials(mode: AccessMode) -> AwsCredentialsCache:
    """Load cached AWS credentials or generate fresh ones from Vault.

    Admin mode does not have a dedicated shared AWS role — it authenticates via
    the admin OIDC role but requests readonly AWS credentials for cluster discovery.
    The admin mode exec-credential path uses ``aws eks get-token --role`` directly
    with the per-cluster admin IAM role rather than Vault-vended STS credentials.
    """
    if mode is AccessMode.ADMIN:
        msg = (
            "Admin mode does not use a shared Vault AWS role. "
            "Use AccessMode.READONLY for cluster discovery in admin setup."
        )
        raise RuntimeError(msg)

    aws_role_name = VAULT_AWS_ROLE_BY_MODE[mode.value]
    aws_cache_path = cache_file(f"aws-creds-{mode.value}")
    cached_payload = load_json(aws_cache_path)
    if cached_payload and cached_payload.get("expires_at"):
        expires_at = parse_expiration(str(cached_payload["expires_at"]))
        if expires_at > json_datetime_now() + timedelta(minutes=5):
            return AwsCredentialsCache(
                access_key=str(cached_payload["access_key"]),
                secret_key=str(cached_payload["secret_key"]),
                session_token=str(cached_payload["session_token"]),
                expires_at=str(cached_payload["expires_at"]),
            )

    token = load_valid_vault_token(mode)
    client = vault_client(token)
    aws_creds = client.secrets.aws.generate_credentials(
        name=aws_role_name,
        ttl=8 * 60 * 60,
        mount_point="aws-mitx",
    )
    expires_at = json_datetime_now() + timedelta(seconds=aws_creds["lease_duration"])
    creds = AwsCredentialsCache(
        access_key=aws_creds["data"]["access_key"],
        secret_key=aws_creds["data"]["secret_key"],
        session_token=aws_creds["data"]["security_token"],
        expires_at=expires_at.isoformat(),
    )
    dump_json(aws_cache_path, asdict(creds))
    return creds


def aws_env_from_credentials(credentials: AwsCredentialsCache) -> dict[str, str]:
    """Build an environment override containing AWS credentials."""
    return {
        **os.environ,
        "AWS_REGION": AWS_REGION,
        "AWS_DEFAULT_REGION": AWS_DEFAULT_REGION,
        "AWS_ACCESS_KEY_ID": credentials.access_key,
        "AWS_SECRET_ACCESS_KEY": credentials.secret_key,
        "AWS_SESSION_TOKEN": credentials.session_token,
    }


@app.command
def setup(
    mode: AccessMode = AccessMode.DEVELOPER,
    current_context: str | None = None,
    output_path: Path = KUBECONFIG_DEFAULT_PATH,
) -> None:
    """Generate a kubeconfig covering all OL EKS clusters.

    Cluster metadata is discovered via the AWS EKS API using Vault-generated
    credentials — no Pulumi CLI or state is required.

    For developer and admin modes each cluster gets two contexts:
      - ``<cluster-name>``          — operator credentials (read/write)
      - ``<cluster-name>-readonly`` — readonly credentials for agents/automation

    For readonly mode a single context per cluster is generated.

    EXAMPLES:
      # Developer access (default) — human read/write + agent readonly contexts
      uv run python scripts/eks/eks.py setup

      # Admin access — cluster-admin + agent readonly contexts
      uv run python scripts/eks/eks.py setup --mode admin

      # Readonly only — for automation or read-only users
      uv run python scripts/eks/eks.py setup --mode readonly
    """
    clusters = fetch_all_cluster_configs(mode)
    if not clusters:
        print("Warning: No EKS clusters found in this AWS account.", file=sys.stderr)

    kube_config = build_kubeconfig(clusters, mode, current_context)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(kube_config, sort_keys=False))

    context_count = len(kube_config.get("contexts", []))
    print(
        f"Wrote kubeconfig: {len(clusters)} clusters, "
        f"{context_count} contexts to {output_path}"
    )
    if mode is not AccessMode.READONLY:
        print(f"  Operator contexts  : {', '.join(c.cluster_name for c in clusters)}")
        print(
            f"  Readonly contexts  : {', '.join(c.cluster_name + '-readonly' for c in clusters)}"
        )


@app.command(name="exec-credential")
def exec_credential(
    cluster_name: str,
    mode: AccessMode,
    admin_role_arn: str | None = None,
) -> None:
    """Emit an ExecCredential document for kubectl/kubeconfig exec auth.

    Called automatically by kubectl via the kubeconfig exec plugin; not intended
    for direct invocation.  Handles Vault OIDC auth, credential generation, and
    local caching transparently.
    """
    command = [
        "aws",
        "eks",
        "get-token",
        "--cluster-name",
        cluster_name,
    ]
    env = os.environ.copy()

    if mode is AccessMode.ADMIN:
        if not admin_role_arn:
            msg = "admin_role_arn is required for admin mode"
            raise RuntimeError(msg)
        command.extend(["--role", admin_role_arn])
    else:
        credentials = load_valid_aws_credentials(mode)
        env = aws_env_from_credentials(credentials)

    token_json = run_command(command, env=env)
    print(token_json, end="")


@app.default
def main() -> None:
    """Show CLI help."""
    app.help_print()


if __name__ == "__main__":
    app()
