#!/usr/bin/env python3
"""Manage kubeconfig generation and exec-based auth for OL EKS clusters."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
import webbrowser
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

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
PULUMI_EKS_PROJECT_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "ol_infrastructure"
    / "infrastructure"
    / "aws"
    / "eks"
)
PRODUCTION_VAULT_ADDRESS = "https://vault-production.odl.mit.edu"
PREFERRED_DEFAULT_CONTEXT = "applications-qa"
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
    """Cluster connection details sourced from Pulumi stack outputs."""

    cluster_name: str
    stack_name: str
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
        code_values = params.get("code")
        if not code_values or not code_values[0]:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing authorization code")
            return

        self.server.token = code_values[0]  # type: ignore[attr-defined]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(SELF_CLOSING_PAGE.encode())

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Silence default HTTP request logging."""


def json_datetime_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(tz=UTC)


def repo_root() -> Path:
    """Return the repository root directory."""
    return Path(__file__).resolve().parents[2]


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


def cluster_name_for_stack(stack_name: str) -> str:
    """Translate a Pulumi stack name into a cluster name."""
    stack_parts = stack_name.split(".")
    return f"{stack_parts[-2]}-{stack_parts[-1].lower()}"


def read_stack_names() -> list[str]:
    """Discover all EKS Pulumi stacks from stack config files."""
    stack_files = sorted(PULUMI_EKS_PROJECT_DIR.glob("Pulumi.*.yaml"))
    return [
        path.name.removeprefix("Pulumi.").removesuffix(".yaml") for path in stack_files
    ]


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


def fetch_cluster_config(stack_name: str) -> ClusterConfig:
    """Fetch cluster connection details from Pulumi outputs."""
    command_output = run_command(
        ["pulumi", "stack", "output", "-j", "-s", stack_name, "kube_config_data"],
        cwd=PULUMI_EKS_PROJECT_DIR,
    )
    output_data = json.loads(command_output)
    return ClusterConfig(
        cluster_name=cluster_name_for_stack(stack_name),
        stack_name=stack_name,
        server=output_data["server"],
        certificate_authority_data=output_data["certificate-authority-data"]["data"],
        admin_role_arn=output_data["admin_role_arn"],
    )


def fetch_all_cluster_configs() -> list[ClusterConfig]:
    """Load all known cluster configs from Pulumi."""
    return [fetch_cluster_config(stack_name) for stack_name in read_stack_names()]


def kubeconfig_exec_args(cluster: ClusterConfig, mode: AccessMode) -> list[str]:
    """Build exec args for kubeconfig user entries."""
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
    """Choose an explicit or sensible default current context."""
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
    mode: AccessMode,
    current_context: str | None,
) -> dict[str, Any]:
    """Build kubeconfig content for all clusters."""
    kube_clusters = []
    contexts = []
    users = []

    for cluster in clusters:
        kube_clusters.append(
            {
                "name": cluster.cluster_name,
                "cluster": {
                    "certificate-authority-data": cluster.certificate_authority_data,
                    "server": cluster.server,
                },
            }
        )
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
                        "args": kubeconfig_exec_args(cluster, mode),
                        "interactiveMode": "IfAvailable",
                        "provideClusterInfo": False,
                    },
                },
            }
        )

    kube_config = {
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
    """Load cached AWS credentials or generate fresh credentials from Vault."""
    if mode is AccessMode.ADMIN:
        msg = "Admin mode does not use Vault-generated shared AWS credentials"
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
    mode: AccessMode = AccessMode.READONLY,
    current_context: str | None = None,
    output_path: Path = KUBECONFIG_DEFAULT_PATH,
) -> None:
    """Generate and write a kubeconfig covering all OL EKS clusters."""
    clusters = fetch_all_cluster_configs()
    kube_config = build_kubeconfig(clusters, mode, current_context)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(kube_config, sort_keys=False))
    print(f"Wrote kubeconfig for {len(clusters)} clusters to {output_path}")


@app.command(name="exec-credential")
def exec_credential(
    cluster_name: str,
    mode: AccessMode,
    admin_role_arn: str | None = None,
) -> None:
    """Emit an ExecCredential document for kubectl/kubeconfig exec auth."""
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
