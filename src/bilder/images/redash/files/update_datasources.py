# A python script that will set datasources in redash
import json
import subprocess
import sys

import yaml

with open("/etc/redash/datasources.yaml") as yamlfile:
    config = yaml.safe_load(yamlfile)

container_id = (
    subprocess.run(
        [
            "/usr/bin/docker",
            "ps",
            "--no-trunc",
            "--filter",
            "name=compose-server",
            "--format",
            "{{.ID}}",
        ],
        capture_output=True,
    )
    .stdout.decode("UTF-8")
    .strip()
)

returncode = 0
for datasource in config["managed_datasources"]:
    update_output = subprocess.run(
        [
            "/usr/bin/docker",
            "exec",
            "-it",
            container_id,
            "python3",
            "manage.py",
            "ds",
            "edit",
            "--options",
            json.dumps(datasource["options"]),
            datasource["name"],
        ],
        capture_output=True,
    )
    if update_output.returncode != 0:
        returncode = update_output.returncode

sys.exit(returncode)
