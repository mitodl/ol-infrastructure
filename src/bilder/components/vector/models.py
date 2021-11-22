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
        Path(__file__).resolve().parent.joinpath("templates", "vector.toml"): {}
    }
    configuration_directory: Path = Path("/etc/vector/")
    data_directory: Path = Path("/var/lib/vector")

    class Config:
        env_prefix = "vector_"
