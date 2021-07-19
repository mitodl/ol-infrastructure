import subprocess
from pathlib import Path
from typing import Any, Dict

import yaml


def read_yaml_secrets(sops_file: Path) -> Dict[str, Any]:
    sops_path = Path(__file__).parent.joinpath("bin", "sops")
    yaml_data = subprocess.run(
        [sops_path, "--decrypt", Path(__file__).parent.joinpath(sops_file)],
        capture_output=True,
    )
    return yaml.safe_load(yaml_data.stdout)
