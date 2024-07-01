import json
import os
import subprocess
from pathlib import Path
from platform import system
from typing import Any

import yaml

if system() == "Darwin":
    SOPS_BINARY = Path(__file__).parent.joinpath("bin", "sops_macos")
else:
    SOPS_BINARY = Path(__file__).parent.joinpath("bin", "sops")


def read_yaml_secrets(sops_file: Path) -> dict[str, Any]:
    yaml_data = subprocess.run(  # noqa: PLW1510, S603
        [
            SOPS_BINARY,
            "--decrypt",
            Path(__file__).parent.joinpath(sops_file),
        ],
        capture_output=True,
    )
    return yaml.safe_load(yaml_data.stdout)


def read_json_secrets(sops_file: Path) -> dict[str, Any]:
    json_data = subprocess.run(  # noqa: PLW1510, S603
        [
            SOPS_BINARY,
            "--decrypt",
            Path(__file__).parent.joinpath(sops_file),
        ],
        capture_output=True,
    )
    return json.loads(json_data.stdout.decode("utf8"))


def set_env_secrets(sops_file: Path) -> None:
    env_data = subprocess.run(  # noqa: PLW1510, S603
        [
            SOPS_BINARY,
            "--decrypt",
            Path(__file__).parent.joinpath(sops_file),
        ],
        capture_output=True,
    )
    for line in env_data.stdout.decode("utf8").split("\n"):
        if "=" in line:
            env_key, env_value = line.split("=", maxsplit=1)
            os.environ[env_key] = env_value
