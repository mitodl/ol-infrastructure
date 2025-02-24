# ruff: noqa: T201, E501

import argparse
import os
import sys
from datetime import datetime, timedelta

import hvac
import yaml

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

ci_vault_client = hvac.Client(url="https://vault-ci.odl.mit.edu")
qa_vault_client = hvac.Client(url="https://vault-qa.odl.mit.edu")
production_vault_client = hvac.Client(url="https://vault-production.odl.mit.edu")

ci_vault_client.auth.github.login(token=os.environ["GITHUB_TOKEN"])
qa_vault_client.auth.github.login(token=os.environ["GITHUB_TOKEN"])
production_vault_client.auth.github.login(token=os.environ["GITHUB_TOKEN"])

if (
    not production_vault_client.is_authenticated()
    or not qa_vault_client.is_authenticated()
    or not ci_vault_client.is_authenticated()
):
    print("""Vault authentication failed.

    Verify you have GITHUB_TOKEN set to a personal access token with read:org permissions""")
    sys.exit(1)


current_clusters = {
    "applications-ci": ci_vault_client,
    "applications-production": ci_vault_client,
    "applications-qa": ci_vault_client,
    "data-ci": ci_vault_client,
    "data-production": ci_vault_client,
    "data-qa": ci_vault_client,
    "operations-ci": ci_vault_client,
    "operations-production": ci_vault_client,
    "operations-qa": ci_vault_client,
}

if production_vault_client.is_authenticated():
    if "aws_creds_parser" in args:
        print("Fetching AWS credentials")
        aws_creds = production_vault_client.secrets.aws.generate_credentials(
            name="eks-cluster-shared-developer-role",
            ttl=args["duration"] * 60,
            mount_point="aws-mitx",
        )
        print(
            f"Credentials expires at: {datetime.now(tz=datetime.now().astimezone().tzinfo) + timedelta(seconds=aws_creds['lease_duration'])}"
        )
        print()
        print('export AWS_REGION="us-east-1"')
        print('export AWS_DEFAULT_REGION="us-east-1"')
        print(f'export AWS_ACCESS_KEY_ID="{aws_creds["data"]["access_key"]}"')
        print(f'export AWS_SECRET_ACCESS_KEY="{aws_creds["data"]["secret_key"]}"')
        print(f'export AWS_SESSION_TOKEN="{aws_creds["data"]["security_token"]}"')
        print()

    elif "kubeconfig_parser" in args:
        contexts = []
        clusters = []
        users = []
        for cluster_name, vault_client in current_clusters.items():
            cluster_data_from_vault = vault_client.secrets.kv.v2.read_secret(
                path=f"eks/kubeconfigs/{cluster_name}", mount_point="secret-global"
            )
            clusters.append(
                {
                    "name": cluster_name,
                    "cluster": {
                        "server": cluster_data_from_vault["data"]["data"]["server"],
                        "certificate-authority-data": cluster_data_from_vault["data"][
                            "data"
                        ]["ca"],
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
        print(yaml.dump(kube_config))
