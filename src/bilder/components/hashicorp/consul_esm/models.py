from pathlib import Path
from typing import Dict, Iterable, Tuple

from pydantic import SecretStr

from bilder.components.hashicorp.models import HashicorpConfig, HashicorpProduct


class ConsulExternalServicesMonitorConfig(HashicorpConfig):
    log_level: str = "INFO"
    consul_service: str = "consul-esm"
    consul_service_tag: str = "consul-esm"
    consul_kv_path: str = "consul-esm/"
    external_node_meta: Dict[str, str] = {"external-node": "true"}
    node_reconnect_timeout: str = "72h"
    node_probe_interval: str = "30s"
    http_addr: str = "localhost:8500"
    token: SecretStr
    ping_type: str = "udp"
    passing_threshold: int = 0
    critical_threshold: int = 5

    class Config:  # noqa: WPS431
        env_prefix = "consul_esm_"


class ConsulExternalServicesMonitor(HashicorpProduct):
    _name: str = "consul-esm"
    version: str = "0.5.0"
    configuration_directory: Path = Path("/etc/consul-esm.d/")

    @property
    def systemd_template_context(self):
        return self

    def render_configuration_files(self) -> Iterable[Tuple[Path, str]]:
        for fpath, config in self.configuration.items():  # noqa: WPS526
            yield self.configuration_directory.joinpath(fpath), config.json(
                exclude_none=True, indent=2
            )
