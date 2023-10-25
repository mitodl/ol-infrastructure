from collections.abc import Iterable
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from bilder.components.hashicorp.models import HashicorpConfig, HashicorpProduct


class ConsulExternalServicesMonitorConfig(HashicorpConfig):
    model_config = SettingsConfigDict(env_prefix="consul_esm_")
    log_level: str = "INFO"
    consul_service: str = "consul-esm"
    consul_service_tag: str = "consul-esm"
    consul_kv_path: str = "consul-esm/"
    external_node_meta: dict[str, str] = {"external-node": "true"}  # noqa: RUF012
    node_reconnect_timeout: str = "72h"
    node_probe_interval: str = "30s"
    http_addr: str = "localhost:8500"
    token: SecretStr
    ping_type: str = "udp"
    passing_threshold: int = 0
    critical_threshold: int = 5


class ConsulExternalServicesMonitor(HashicorpProduct):
    _name: str = "consul-esm"
    version: str = "0.5.0"
    configuration_directory: Path = Path("/etc/consul-esm.d/")
    systemd_execution_type: str = "notify"

    @property
    def systemd_template_context(self):
        return self

    def render_configuration_files(self) -> Iterable[tuple[Path, str]]:
        for fpath, config in self.configuration.items():
            yield (
                self.configuration_directory.joinpath(fpath),
                config.model_dump_json(exclude_none=True, indent=2),
            )
