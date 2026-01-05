import argparse
import json
import logging
import os
from pathlib import Path

import boto3
import requests
from requests_aws4auth import AWS4Auth

from bridge.secrets.sops import read_yaml_secrets

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

parser = argparse.ArgumentParser()
parser.add_argument("-s", "--stack", help="pulumi stack name", required=True)
parser.add_argument(
    "--dry-run",
    action="store_true",
    help="preview changes without applying them",
)
parser.add_argument(
    "--verbose",
    "-v",
    action="store_true",
    help="enable verbose logging",
)
parser.add_argument(
    "--model-name",
    type=str,
    default="hybrid_search_model",
)
parser.add_argument(
    "--openai-model",
    type=str,
    default="text-embedding-3-large",
    help="OpenAI embedding model to use (e.g., text-embedding-3-large)",
)
args = vars(parser.parse_args())

if args["verbose"]:
    logger.setLevel(logging.DEBUG)

dry_run = args["dry_run"]
if dry_run:
    logger.info("DRY RUN MODE: No changes will be applied")

env_prefix = args["stack"].split(".")[-2]
env_suffix = args["stack"].split(".")[-1].lower()

logger.info("Processing stack: %s", args["stack"])
logger.info("Environment: %s.%s", env_prefix, env_suffix)

master_username = "opensearch"
secrets_path = Path(f"opensearch/opensearch.{env_prefix}.{env_suffix}.yaml")
logger.debug("Reading secrets from: %s", secrets_path)

master_password = read_yaml_secrets(secrets_path)["master_user_password"]
logger.debug("Successfully loaded master password")

logger.info("Fetching cluster endpoint from Pulumi stack output")
stream = os.popen(f"pulumi stack output -s {args['stack']} --json")  # noqa: S605
stack_output = json.loads(stream.read())
cluster = stack_output["cluster"]
logger.info("Cluster endpoint: %s", cluster["endpoint"])


assume_role_response = (
    boto3.Session()
    .client("sts")
    .assume_role(
        RoleArn=stack_output["connector_management_role_arn"],
        RoleSessionName="session_name",
    )
)
credentials = assume_role_response["Credentials"]
awsauth = AWS4Auth(
    credentials["AccessKeyId"],
    credentials["SecretAccessKey"],
    "us-east-1",
    "es",
    session_token=credentials["SessionToken"],
)


connector_name = f"{args['model_name']}_connector"

connector_payload = {
    "name": connector_name,
    "description": "Connector for OpenAI embedding models",
    "version": "1.0",
    "protocol": "http",
    "credential": {
        "secretArn": stack_output["openai_secret_arn"],
        "roleArn": stack_output["openai_connector_role_arn"],
    },
    "parameters": {
        "model": args["openai_model"],
    },
    "actions": [
        {
            "action_type": "predict",
            "method": "POST",
            "url": "https://api.openai.com/v1/embeddings",
            "headers": {"Authorization": "Bearer ${credential.secretArn.api_key}"},
            "request_body": '{"input": ${parameters.input}, "model": "${parameters.model}" }',  # noqa: E501
            "pre_process_function": "connector.pre_process.openai.embedding",
            "post_process_function": "connector.post_process.openai.embedding",
        },
    ],
}

logger.info("Connector payload: %s", json.dumps(connector_payload, indent=2))

logger.info("Configuring connector...")

url = f"https://{cluster['endpoint']}/_plugins/_ml/connectors/_create"
action = "[DRY RUN] Would create" if dry_run else "Creating"
logger.info("%s connector: %s", action, connector_name)

headers = {"Content-Type": "application/json", "Connection": "close"}

if not dry_run:
    response = requests.post(  # noqa: S113
        url,
        headers=headers,
        auth=awsauth,
        data=json.dumps(connector_payload),
    )
    if response.status_code in (200, 201):
        logger.info("Successfully created connector: %s", connector_name)
    else:
        logger.error(
            "Failed to create connector %s: %s - %s",
            connector_name,
            response.status_code,
            response.text,
        )

if dry_run:
    logger.info("DRY RUN COMPLETE: No changes were applied")
else:
    logger.info("Configuration complete")
