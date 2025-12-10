import argparse
import json
import logging
import os
from pathlib import Path

import requests

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
stream = os.popen(f"pulumi stack output -s {args['stack']} 'cluster'")  # noqa: S605
cluster_output = stream.read()
cluster = json.loads(cluster_output)
logger.info("Cluster endpoint: %s", cluster["endpoint"])

read_only_role = {
    "cluster_permissions": ["cluster_composite_ops_ro"],
    "index_permissions": [
        {
            "index_patterns": [
                "*"
            ],  # TODO: Define actual indices  # noqa: FIX002, TD002
            "allowed_actions": [
                "read"
            ],  # TODO: Confirm this is all that is needed  # noqa: FIX002, TD002
        }
    ],
}
read_write_role = {
    "cluster_permissions": [
        "cluster_composite_ops",
        "cluster_monitor",
        "indices:data/read/scroll",
        "indices:data/read/scroll/clear",
        "cluster:admin/opensearch/ml/*",
        "cluster:admin/ingest/pipeline/*",
        "cluster:admin/search/pipeline/get",
        "cluster:admin/search/pipeline/put",
    ],
    "index_permissions": [
        {
            "index_patterns": [
                "*"
            ],  # TODO: Define actual indices  # noqa: FIX002, TD002
            "allowed_actions": [
                "crud",
                "create_index",
                "indices_all",
                "indices:data/read/scroll",
                "indices:data/read/scroll/clear",
                "indices:data/read/scroll*",
                "indices:data/read/scroll/clear*",
            ],  # TODO: Confirm this is all that is needed  # noqa: FIX002, TD002
        }
    ],
}

logger.debug("Loading user passwords from secrets")
read_only_user = {
    "password": read_yaml_secrets(secrets_path)["read_only_user_password"],
    "opendistro_security_roles": ["read_only_role"],
}
read_write_user = {
    "password": read_yaml_secrets(secrets_path)["read_write_user_password"],
    "opendistro_security_roles": ["read_write_role"],
}

headers = {"Content-Type": "application/json", "Connection": "close"}
auth = requests.auth.HTTPBasicAuth(master_username, master_password)

roles = {
    "read_only_role": read_only_role,
    "read_write_role": read_write_role,
}
users = {
    "read_only_user": read_only_user,
    "read_write_user": read_write_user,
}

role_mappings = {
    "read_only_role": {
        "hosts": [],
        "users": ["read_only_user"],
        "backend_roles": [],
        "and_backend_roles": [],
    },
    "read_write_role": {
        "hosts": [],
        "users": ["read_write_user"],
        "backend_roles": [],
        "and_backend_roles": [],
    },
}

logger.info("Configuring roles...")
for r_name, r_def in roles.items():
    url = f"https://{cluster['endpoint']}/_plugins/_security/api/roles/{r_name}"
    action = "[DRY RUN] Would create" if dry_run else "Creating"
    logger.info("%s role: %s", action, r_name)
    logger.debug("Role definition: %s", json.dumps(r_def, indent=2))

    if not dry_run:
        response = requests.put(  # noqa: S113
            url,
            headers=headers,
            auth=auth,
            data=json.dumps(r_def),
        )
        if response.status_code in (200, 201):
            logger.info("Successfully created role: %s", r_name)
        else:
            logger.error(
                "Failed to create role %s: %s - %s",
                r_name,
                response.status_code,
                response.text,
            )

logger.info("Configuring users...")
for u_name, u_def in users.items():
    url = f"https://{cluster['endpoint']}/_plugins/_security/api/internalusers/{u_name}"
    # Redact password in logs
    safe_u_def = {**u_def, "password": "***REDACTED***"}
    action = "[DRY RUN] Would create" if dry_run else "Creating"
    logger.info("%s user: %s", action, u_name)
    logger.debug("User definition: %s", json.dumps(safe_u_def, indent=2))

    if not dry_run:
        response = requests.put(  # noqa: S113
            url,
            headers=headers,
            auth=auth,
            data=json.dumps(u_def),
        )
        if response.status_code in (200, 201):
            logger.info("Successfully created user: %s", u_name)
        else:
            logger.error(
                "Failed to create user %s: %s - %s",
                u_name,
                response.status_code,
                response.text,
            )

logger.info("Configuring role mappings...")
for r_name, rm in role_mappings.items():
    url = f"https://{cluster['endpoint']}/_plugins/_security/api/rolesmapping/{r_name}"
    action = "[DRY RUN] Would map" if dry_run else "Mapping"
    logger.info("%s role: %s", action, r_name)
    logger.debug("Role mapping: %s", json.dumps(rm, indent=2))

    if not dry_run:
        response = requests.put(  # noqa: S113
            url,
            headers=headers,
            auth=auth,
            data=json.dumps(rm),
        )
        if response.status_code in (200, 201):
            logger.info("Successfully mapped role: %s", r_name)
        else:
            logger.error(
                "Failed to map role %s: %s - %s",
                r_name,
                response.status_code,
                response.text,
            )

if dry_run:
    logger.info("DRY RUN COMPLETE: No changes were applied")
else:
    logger.info("Configuration complete")
