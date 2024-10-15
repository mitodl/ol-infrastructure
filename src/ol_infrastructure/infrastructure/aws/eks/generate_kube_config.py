# ruff:  noqa: E501
import argparse
import glob
import json
import os
from pathlib import Path

import yaml

parser = argparse.ArgumentParser()
parser.add_argument(
    "-c",
    "--current-context",
    help="sets a current context in the rendered kube_config file",
    required=False,
)
args = parser.parse_args()


contexts = []
clusters = []
users = []


def extract_kube_config_params(admin_arn, cluster_ca, cluster_endpoint, cluster_name):
    clusters.append(
        {
            "name": cluster_name,
            "cluster": {
                "certificate-authority-data": cluster_ca,
                "server": cluster_endpoint,
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
                    "args": [
                        "eks",
                        "get-token",
                        "--cluster-name",
                        cluster_name,
                        "--role",
                        admin_arn,
                    ],
                    "command": "aws",
                    "env": [
                        {
                            "name": "KUBERNETES_EXEC_INFO",
                            "value": '{"apiVersion": "client.authentication.k8s.io/v1beta1"}',
                        },
                    ],
                    "interactiveMode": "IfAvailable",
                    "provideClusterInfo": False,
                },
            },
        }
    )


stack_defs = glob.glob(  # noqa: PTH207
    str(Path(__file__).parent.joinpath("Pulumi.infrastructure.*.yaml"))
)

for stack in stack_defs:
    stack_name = str(Path(stack).name).removeprefix("Pulumi.").removesuffix(".yaml")
    cluster_name = stack_name.split(".")[-2] + "-" + stack_name.split(".")[-1].lower()

    stream = os.popen(f"pulumi stack output -j -s {stack_name} 'kube_config_data'")  # noqa: S605
    command_output = stream.read()
    output_data = json.loads(command_output)

    extract_kube_config_params(
        admin_arn=output_data["role_arn"],
        cluster_ca=output_data["certificate-authority-data"]["data"],
        cluster_endpoint=output_data["server"],
        cluster_name=cluster_name,
    )

kube_config = {
    "apiVersion": "v1",
    "kind": "Config",
    "preferences": {},
    "clusters": clusters,
    "contexts": contexts,
    "users": users,
}
if args.current_context:
    kube_config["current-context"] = args.current_context

print(yaml.dump(kube_config))  # noqa: T201
