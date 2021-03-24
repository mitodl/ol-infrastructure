from typing import Dict

from pydantic import SecretStr

from bilder.components.hashicorp.models import HashicorpConfig


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
