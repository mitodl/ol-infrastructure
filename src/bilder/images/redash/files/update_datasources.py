# A python script that will set datasources in redash  # noqa: INP001
import json
import subprocess
import sys
import time

import yaml

# Pause for 15 seconds to give docker-compose time to
# restart containers if that is triggered
time.sleep(15)

with open("/etc/redash/datasources.yaml") as yamlfile:  # noqa: PTH123
    config = yaml.safe_load(yamlfile)

container_id = (
    subprocess.run(  # noqa: PLW1510
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
    update_output = subprocess.run(  # noqa: PLW1510, S603
        [
            "/usr/bin/docker",
            "exec",
            "-t",
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
