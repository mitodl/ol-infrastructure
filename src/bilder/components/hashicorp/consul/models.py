from pathlib import Path
from typing import Dict, List, Optional

from pydantic.fields import Field
from pydantic.types import SecretStr

from bilder.components.hashicorp.models import FlexibleBaseModel, HashicorpConfig


class EnvConsulConfig(HashicorpConfig):
    class Config:  # noqa: WPS431
        env_prefix = "envconsul_"


class ConsulACLToken(FlexibleBaseModel):
    master: str


class ConsulACL(FlexibleBaseModel):
    tokens: Optional[List[ConsulACLToken]]


class ConsulAddresses(FlexibleBaseModel):
    dns: Optional[str] = "0.0.0.0"  # noqa: S104
    http: Optional[str] = "0.0.0.0"  # noqa: S104
    https: Optional[str]
    grpc: Optional[str]


class ConsulDNSConfig(FlexibleBaseModel):
    allow_stale: bool = True
    node_ttl: str = "30s"
    service_ttl: Dict[str, str] = {"*": "30s"}


class ConsulServiceCheck(FlexibleBaseModel):
    tcp: Optional[int]
    udp: Optional[int]
    interval: str


class ConsulService(FlexibleBaseModel):
    name: str
    port: int
    address: str
    check: ConsulServiceCheck


class ConsulTelemetry(FlexibleBaseModel):
    dogstatsd_addr: str = "127.0.0.1:8125"


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


class ConsulTemplateConfig(HashicorpConfig):
    class Config:  # noqa: WPS431
        env_prefix = "consul_template_"


class ConsulConfig(HashicorpConfig):
    acl: Optional[ConsulACL]
    addresses: Optional[ConsulAddresses]
    bootstrap_expect: Optional[int] = 3
    client_addr: Optional[str]
    datacenter: Optional[str]
    disable_host_node_id: Optional[bool] = True
    dns_config: Optional[ConsulDNSConfig]
    enable_syslog: Optional[bool] = True
    leave_on_terminate: Optional[bool] = False
    log_level: Optional[str] = "WARN"
    primary_datacenter: Optional[str]
    recursors: Optional[List[str]] = Field(
        None,
        description="List of DNS servers to use for resolving non-consul addresses",
    )
    rejoin_after_leave: Optional[bool] = True
    retry_join: Optional[str]
    retry_join_wan: Optional[str]
    server: Optional[bool] = False
    service: Optional[ConsulService]
    services: Optional[List[ConsulService]]
    skip_leave_on_interrupt: Optional[bool] = True
    telemetry: Optional[ConsulTelemetry]
    ui: Optional[bool] = False

    class Config:  # noqa: WPS431
        env_prefix = "consul_"


class Consul(HashicorpConfig):
    config_directory: Path = Path("/etc/consul.d/")
    config: Dict[Path, ConsulConfig]
    user: str = "consul"
    data_directory: Path = Path("/var/lib/consul")
    systemd_template: Path = Path(__file__).parent.joinpath(
        "templates", "consul.service.j2"
    )
