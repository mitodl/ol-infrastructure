# ruff: noqa: T201, E501

import argparse
import logging
import sys
import urllib.parse
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import hvac
import yaml

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stderr)  # Log to stderr to avoid mixing with output
    ],
)
logger = logging.getLogger(__name__)

# From hvac OIDC documentation
OIDC_CALLBACK_PORT = 8250
OIDC_REDIRECT_URI = f"http://localhost:{OIDC_CALLBACK_PORT}/oidc/callback"
SELF_CLOSING_PAGE = """
<!doctype html>
<html>
<head>
<script>
// Closes IE, Edge, Chrome, Brave
window.onload = function load() {
  window.open('', '_self', '');
  window.close();
};
</script>
</head>
<body>
  <p>Authentication successful, you can close the browser now.</p>
  <script>
    // Needed for Firefox security
    setTimeout(function() {
          window.close()
    }, 5000);
  </script>
</body>
</html>
"""


# handles the callback
def login_oidc_get_token():
    class HttpServ(HTTPServer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.token = None

    class AuthHandler(BaseHTTPRequestHandler):
        token = ""

        def do_GET(self):
            params = urllib.parse.parse_qs(self.path.split("?")[1])
            self.server.token = params["code"][0]  # type: ignore[attr-defined]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(str.encode(SELF_CLOSING_PAGE))

    server_address = ("", OIDC_CALLBACK_PORT)
    httpd = HttpServ(server_address, AuthHandler)
    httpd.handle_request()
    return httpd.token


def oidc_login(client: hvac.Client, role: str):
    auth_url_response = client.auth.oidc.oidc_authorization_url_request(
        role=role,
        redirect_uri=OIDC_REDIRECT_URI,
    )
    auth_url = auth_url_response["data"]["auth_url"]
    if auth_url == "":
        msg = "Unable to retrieve auth URL from Vault"
        raise RuntimeError(msg)

    params = urllib.parse.parse_qs(auth_url.split("?")[1])
    auth_url_nonce = params["nonce"][0]
    auth_url_state = params["state"][0]

    webbrowser.open(auth_url)
    code = login_oidc_get_token()

    auth_result = client.auth.oidc.oidc_callback(
        code=code,
        nonce=auth_url_nonce,
        state=auth_url_state,
    )
    new_token = auth_result["auth"]["client_token"]
    client.token = new_token


parser = argparse.ArgumentParser(description="EKS Login Helper")
subparsers = parser.add_subparsers(help="functions")

aws_creds_parser = subparsers.add_parser(
    "aws_creds",
    help="Fetch AWS credentials",
    description="Fetches AWS credentials for the shared EKS developer role and echos `export` statements to stdout",
)
aws_creds_parser.set_defaults(aws_creds_parser=True)
aws_creds_parser.add_argument(
    "-d",
    "--duration",
    help="Lease duration in minutes of the AWS credentials. 8 hour (480 minute) max",
    required=False,
    default=60,
    type=int,
)

kubeconfig_parser = subparsers.add_parser(
    "kubeconfig",
    help="Generate kubeconfig file",
    description="Generates a kubeconfig file for all active EKS clusters and echoes the file to stdout",
)
kubeconfig_parser.set_defaults(kubeconfig_parser=True)
kubeconfig_parser.add_argument(
    "-c",
    "--set-current-context",
    help="Sets a current context in the rendered kubeconfig file",
    required=False,
)

kubeconfig_parser.add_argument(
    "-r",
    "--role",
    help="Set the OIDC role to use for authenticating to Vault (developer or admin)",
    required=False,
    default="developer",
)

args = vars(parser.parse_args())

logger.info("Initializing Vault clients")
ci_vault_client = hvac.Client(url="https://vault-ci.odl.mit.edu")
qa_vault_client = hvac.Client(url="https://vault-qa.odl.mit.edu")
production_vault_client = hvac.Client(url="https://vault-production.odl.mit.edu")

role = args.get("role", "developer")
try:
    logger.info("Authenticating with CI Vault via OIDC (role: %s)...", role)
    oidc_login(ci_vault_client, role)
    logger.info("Authenticating with QA Vault via OIDC (role: %s)...", role)
    oidc_login(qa_vault_client, role)
    logger.info("Authenticating with Production Vault via OIDC (role: %s)...", role)
    oidc_login(production_vault_client, role)
except Exception:
    logger.exception("Failed to authenticate with Vault")
    sys.exit(1)

# Check authentication status for each client individually
vault_clients = {
    "production": production_vault_client,
    "qa": qa_vault_client,
    "ci": ci_vault_client,
}

failed_clients = []
for client_name, client in vault_clients.items():
    if client.is_authenticated():
        logger.info("Successfully authenticated with %s Vault", client_name)
    else:
        logger.error("Failed to authenticate with %s Vault", client_name)
        failed_clients.append(client_name)

if failed_clients:
    logger.error("Vault authentication failed for: %s", ", ".join(failed_clients))
    logger.error(
        "Re-run and complete the browser login. If it keeps failing, confirm your "
        "Keycloak account maps to the '%s' Vault OIDC role.",
        role,
    )
    sys.exit(1)

logger.info("Successfully authenticated with all Vault instances")

current_clusters = {
    "applications-ci": ci_vault_client,
    "applications-production": production_vault_client,
    "applications-qa": qa_vault_client,
    "data-ci": ci_vault_client,
    "data-production": production_vault_client,
    "data-qa": qa_vault_client,
    "operations-ci": ci_vault_client,
    "operations-production": production_vault_client,
    "operations-qa": qa_vault_client,
    "residential-ci": ci_vault_client,
    "residential-production": production_vault_client,
    "residential-qa": qa_vault_client,
}

if production_vault_client.is_authenticated():
    if "aws_creds_parser" in args:
        logger.info(
            "Fetching AWS credentials with %s minute duration", args["duration"]
        )
        try:
            aws_creds = production_vault_client.secrets.aws.generate_credentials(
                name="eks-cluster-shared-developer-role",
                ttl=args["duration"] * 60,
                mount_point="aws-mitx",
            )
            expiry_time = datetime.now(
                tz=datetime.now().astimezone().tzinfo
            ) + timedelta(seconds=aws_creds["lease_duration"])
            logger.info(
                "AWS credentials generated successfully, expires at: %s", expiry_time
            )

            # Output to stdout for shell evaluation
            print('export AWS_REGION="us-east-1"')
            print('export AWS_DEFAULT_REGION="us-east-1"')
            print(f'export AWS_ACCESS_KEY_ID="{aws_creds["data"]["access_key"]}"')
            print(f'export AWS_SECRET_ACCESS_KEY="{aws_creds["data"]["secret_key"]}"')
            print(f'export AWS_SESSION_TOKEN="{aws_creds["data"]["security_token"]}"')

        except Exception:
            logger.exception("Failed to fetch AWS credentials")
            sys.exit(1)

    elif "kubeconfig_parser" in args:
        logger.info("Generating kubeconfig for %s clusters", len(current_clusters))
        contexts = []
        clusters = []
        users = []

        for cluster_name, vault_client in current_clusters.items():
            logger.debug("Processing cluster: %s", cluster_name)
            try:
                cluster_data_from_vault = vault_client.secrets.kv.v2.read_secret(
                    path=f"eks/kubeconfigs/{cluster_name}", mount_point="secret-global"
                )

                clusters.append(
                    {
                        "name": cluster_name,
                        "cluster": {
                            "server": cluster_data_from_vault["data"]["data"]["server"],
                            "certificate-authority-data": cluster_data_from_vault[
                                "data"
                            ]["data"]["ca"],
                        },
                    }
                )
                contexts.append(
                    {
                        "name": cluster_name,
                        "context": {
                            "cluster": cluster_name,
                            "user": cluster_name,
                        },
                    }
                )
                users.append(
                    {
                        "name": cluster_name,
                        "user": {
                            "exec": {
                                "apiVersion": "client.authentication.k8s.io/v1beta1",
                                "command": "aws",
                                "args": [
                                    "eks",
                                    "get-token",
                                    "--cluster-name",
                                    cluster_name,
                                ],
                                "env": [
                                    {
                                        "name": "KUBERNETES_EXEC_INFO",
                                        "value": '{"apiVersion": "client.authentication.k8s.io/v1beta1"}',
                                    }
                                ],
                                "interactiveMode": "IfAvailable",
                                "provideClusterInfo": False,
                            },
                        },
                    }
                )
                logger.debug("Successfully processed cluster: %s", cluster_name)

            except Exception:
                logger.exception("Failed to process cluster %s", cluster_name)
                continue

        kube_config = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": clusters,
            "contexts": contexts,
            "users": users,
            "preferences": {},
        }

        if args.get("set_current_context"):
            kube_config["current-context"] = args["set_current_context"]
            logger.info("Set current context to: %s", args["set_current_context"])

        logger.info("Kubeconfig generated successfully")
        print(yaml.dump(kube_config))
