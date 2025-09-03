# ruff: noqa: T201, E501

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

import hvac
import requests
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

# Constants
HTTP_OK = 200
HTTP_NOT_FOUND = 404
REQUEST_TIMEOUT = 10


def check_github_team_membership(token, org, team_slug, required_teams=None):
    """
    Check if the GitHub token belongs to a user who is a member of any of the specified team(s).

    Args:
        token (str): GitHub personal access token
        org (str): GitHub organization name
        team_slug (str): GitHub team slug to check membership for (unused, kept for compatibility)
        required_teams (list): List of team slugs that user can be a member of (OR logic)

    Returns:
        bool: True if user is a member of at least one required team, False otherwise
    """
    if required_teams is None:
        required_teams = [team_slug]

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    user_teams = []
    missing_teams = []

    try:
        # Get current user info
        user_response = requests.get(
            "https://api.github.com/user",
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        user_response.raise_for_status()
        username = user_response.json()["login"]
        logger.info("Checking team membership for user: %s", username)

        # Check membership for each required team
        for team in required_teams:
            membership_url = (
                f"https://api.github.com/orgs/{org}/teams/{team}/memberships/{username}"
            )
            response = requests.get(
                membership_url,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == HTTP_OK:
                membership_data = response.json()
                if membership_data.get("state") == "active":
                    logger.info(
                        "User %s is an active member of team: %s", username, team
                    )
                    user_teams.append(team)
                else:
                    logger.warning(
                        "User %s has pending membership in team: %s", username, team
                    )
                    missing_teams.append(team)
            elif response.status_code == HTTP_NOT_FOUND:
                logger.debug("User %s is not a member of team: %s", username, team)
                missing_teams.append(team)
            else:
                logger.error(
                    "Failed to check membership for team %s: HTTP %s",
                    team,
                    response.status_code,
                )
                missing_teams.append(team)

        # Check if user is a member of at least one required team (OR logic)
        if user_teams:
            logger.info("User %s is a member of teams: %s", username, user_teams)
            return True
        else:
            # Log specific messages for missing team access
            logger.error(
                "Access denied: User %s is not a member of any required GitHub teams.",
                username,
            )
            logger.error(
                "Required teams (need membership in at least one): %s", required_teams
            )
            if any(
                team in ["vault-developer-access", "vault-devops-access"]
                for team in required_teams
            ):
                logger.error(
                    "Please contact DevOps to be added to the appropriate GitHub teams for Vault access."
                )
            return False

    except requests.exceptions.RequestException:
        logger.exception("Failed to check GitHub team membership")
        return False
    except KeyError:
        logger.exception("Unexpected response format from GitHub API")
        return False


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

args = vars(parser.parse_args())

# Check GitHub team membership before proceeding
logger.info("Verifying GitHub team membership")
try:
    github_token = os.environ["GITHUB_TOKEN"]

    # Define required teams for access
    required_teams = ["vault-developer-access", "vault-devops-access"]

    if not check_github_team_membership(
        github_token, "mitodl", "vault-developer-access", required_teams
    ):
        logger.error("Access denied: User is not a member of required GitHub teams")
        sys.exit(1)

except KeyError:
    logger.exception("GITHUB_TOKEN environment variable not found")
    sys.exit(1)

logger.info("Initializing Vault clients")
ci_vault_client = hvac.Client(url="https://vault-ci.odl.mit.edu")
qa_vault_client = hvac.Client(url="https://vault-qa.odl.mit.edu")
production_vault_client = hvac.Client(url="https://vault-production.odl.mit.edu")

logger.info("Authenticating with Vault using GitHub token")
try:
    logger.debug("Authenticating with CI Vault...")
    ci_vault_client.auth.github.login(token=github_token)
    logger.debug("Authenticating with QA Vault...")
    qa_vault_client.auth.github.login(token=github_token)
    logger.debug("Authenticating with Production Vault...")
    production_vault_client.auth.github.login(token=github_token)
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
        "Verify you have GITHUB_TOKEN set to a personal access token with read:org permissions"
    )
    logger.error(
        "Also ensure your GitHub account has access to the required teams for Vault authentication"
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
