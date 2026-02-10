from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic_settings import SettingsConfigDict

from bilder.lib.model_helpers import OLBaseSettings


class VectorInstallMethod(StrEnum):
    package = "package"


class VectorConfig(OLBaseSettings):
    model_config = SettingsConfigDict(env_prefix="vector_")
    install_method: VectorInstallMethod = VectorInstallMethod.package
    user: str = "vector"
    configuration_templates: dict[Path, dict[str, Any]] = {  # noqa: RUF012
        Path(__file__).resolve().parent.joinpath("templates", "vector.toml"): {},
        Path(__file__).resolve().parent.joinpath("templates", "host_metrics.yaml"): {},
    }
    configuration_directory: Path = Path("/etc/vector/")
    data_directory: Path = Path("/var/lib/vector")
    is_proxy: bool = False
    is_docker: bool = False
    use_global_log_sink: bool = False
    use_global_metric_sink: bool = False
    tls_config_directory: Path = Path("/etc/vector/ssl/")
