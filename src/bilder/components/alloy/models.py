from enum import Enum
from pathlib import Path
from typing import Any

from bilder.lib.model_helpers import OLBaseSettings


# Other options may someday include 'docker'
class AlloyInstallMethod(str, Enum):
    method = "apt"


class AlloyConfig(OLBaseSettings):
    apt_repo_url: str = "https://apt.grafana.com"
    clear_default_config: bool = True
    configuration_directory: Path = Path("/etc/alloy")
    configuration_templates: dict[Path, dict[str, Any]] = {  # noqa: RUF012
        Path(__file__).resolve().parent.joinpath("files", "otel-collector.config"): {},
    }
    gpg_key_url: str = "https://apt.grafana.com/gpg.key"
    install_method: AlloyInstallMethod = AlloyInstallMethod.method
    keyring_path: Path = Path("/etc/apt/keyrings/grafana.gpg")
    sources_list_path: Path = Path("/etc/apt/sources.list.d/grafana.list")
    user: str = "alloy"
