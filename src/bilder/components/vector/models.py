from enum import Enum
from pathlib import Path
from typing import Any, Dict

from bilder.lib.model_helpers import OLBaseSettings


class VectorInstallMethod(str, Enum):  # noqa: WPS600
    package = "package"


class VectorConfig(OLBaseSettings):
    install_method: VectorInstallMethod = VectorInstallMethod.package
    user: str = "vector"
    configuration_templates: Dict[Path, Dict[str, Any]] = {
        Path(__file__).resolve().parent.joinpath("templates", "vector.toml"): {},
        Path(__file__).resolve().parent.joinpath("templates", "host_metrics.yaml"): {},
    }
    configuration_directory: Path = Path("/etc/vector/")
    data_directory: Path = Path("/var/lib/vector")
    is_proxy: bool = False
    tls_config_dir: str = "/etc/ssl/private/vector"

    class Config:
        env_prefix = "vector_"
