import argparse
import json
import os
from pathlib import Path

import requests

from bridge.secrets.sops import read_yaml_secrets

parser = argparse.ArgumentParser()
parser.add_argument("-s", "--stack", help="pulumi stack name", required=True)
args = vars(parser.parse_args())

env_prefix = args["stack"].split(".")[-2]
env_suffix = args["stack"].split(".")[-1].lower()


master_username = "opensearch"
master_password = read_yaml_secrets(
    Path(f"opensearch/opensearch.{env_prefix}.{env_suffix}.yaml")
)["master_user_password"]

stream = os.popen(f"pulumi stack output -s {args['stack']} 'cluster'")  # noqa: S605
cluster_output = stream.read()
cluster = json.loads(cluster_output)

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

read_only_user = {
    "password": read_yaml_secrets(
        Path(f"opensearch/opensearch.{env_prefix}.{env_suffix}.yaml")
    )["read_only_user_password"],
    "opendistro_security_roles": ["read_only_role"],
}
read_write_user = {
    "password": read_yaml_secrets(
        Path(f"opensearch/opensearch.{env_prefix}.{env_suffix}.yaml")
    )["read_write_user_password"],
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

for r_name, r_def in roles.items():
    url = f"https://{cluster['endpoint']}/_plugins/_security/api/roles/{r_name}"
    response = requests.put(  # noqa: S113
        url,
        headers=headers,
        auth=auth,
        data=json.dumps(r_def),
    )

for u_name, u_def in users.items():
    url = f"https://{cluster['endpoint']}/_plugins/_security/api/internalusers/{u_name}"
    response = requests.put(  # noqa: S113
        url,
        headers=headers,
        auth=auth,
        data=json.dumps(u_def),
    )

for r_name, rm in role_mappings.items():
    url = f"https://{cluster['endpoint']}/_plugins/_security/api/rolesmapping/{r_name}"
    response = requests.put(  # noqa: S113
        url,
        headers=headers,
        auth=auth,
        data=json.dumps(rm),
    )
