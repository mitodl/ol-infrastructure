import argparse
import json
import os
from pathlib import Path

import requests

from bridge.secrets.sops import read_yaml_secrets

# Documentation on request formats:
# https://opendistro.github.io/for-elasticsearch-docs/old/0.9.0/docs/security/api/#create-role

parser = argparse.ArgumentParser()
parser.add_argument("-s", "--stack", help="pulumi stack name", required=True)
args = vars(parser.parse_args())

env_prefix = args["stack"].split(".")[-2]
env_suffix = args["stack"].split(".")[-1]

master_username = "opensearch"
master_password = read_yaml_secrets(
    Path(f"opensearch/opensearch.{env_prefix}.{env_suffix}.yaml")
)["master_user_password"]

stream = os.popen(f"pulumi stack output -s {args['stack']} 'cluster'")
cluster_output = stream.read()
cluster = json.loads(cluster_output)

read_only_role = {
    "cluster": ["cluster_composite_ops_ro"],
    "indices": {
        "*": {
            "*": ["READ"],
        }
    },
    "tenants": {},
}

read_write_role = {
    "cluster": [
        "cluster_composite_ops",
        "indices:data/read/scroll",
        "indices:data/read/scroll/clear",
    ],
    "indices": {
        "*": {
            "*": [
                "crud",
                "create_index",
                "indices_all",
                "indices:data/read/scroll",
                "indices:data/read/scroll/clear",
                "indices:data/read/scroll*",
                "indices:data/read/scroll/clear*",
            ],
        }
    },
}

read_only_user = {
    "password": read_yaml_secrets(
        Path(f"opensearch/opensearch.{env_prefix}.{env_suffix}.yaml")
    )["read_only_user_password"],
    "roles": ["read_only_role"],
}
read_write_user = {
    "password": read_yaml_secrets(
        Path(f"opensearch/opensearch.{env_prefix}.{env_suffix}.yaml")
    )["read_write_user_password"],
    "roles": ["read_write_role"],
}

# headers = {"Content-Type": "application/json", "Connection": "close"}
headers = {"Content-Type": "application/json"}
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
    },
    "read_write_role": {
        "hosts": [],
        "users": ["read_write_user"],
    },
}

for r_name, r_def in roles.items():
    url = f"https://{cluster['endpoint']}/_opendistro/_security/api/roles/{r_name}"
    response = requests.put(url, headers=headers, auth=auth, data=json.dumps(r_def))
    print(response.text)

for u_name, u_def in users.items():
    url = f"https://{cluster['endpoint']}/_opendistro/_security/api/internalusers/{u_name}"
    response = requests.put(url, headers=headers, auth=auth, data=json.dumps(u_def))
    print(response.text)

for r_name, rm in role_mappings.items():
    url = (
        f"https://{cluster['endpoint']}/_opendistro/_security/api/rolesmapping/{r_name}"
    )
    response = requests.put(url, headers=headers, auth=auth, data=json.dumps(rm))
    print(response.text)
