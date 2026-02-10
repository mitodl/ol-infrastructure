from enum import StrEnum
from pathlib import Path

from pydantic_settings import SettingsConfigDict

from bilder.lib.model_helpers import OLBaseSettings


# Other options may someday include 'docker'
class AlloyInstallMethod(StrEnum):
    method = "apt"


class AlloyConfig(OLBaseSettings):
    model_config = SettingsConfigDict(env_prefix="alloy_")
    apt_repo_url: str = "https://apt.grafana.com"
    clear_default_config: bool = True
    configuration_directory: Path = Path("/etc/alloy")
    configuration_file: Path = configuration_directory.joinpath("config.alloy")
    gpg_key_url: str = "https://apt.grafana.com/gpg.key"
    install_method: AlloyInstallMethod = AlloyInstallMethod.method
    keyring_path: Path = Path("/etc/apt/keyrings/grafana.gpg")
    sources_list_path: Path = Path("/etc/apt/sources.list.d/grafana.list")
    user: str = "alloy"
