from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from pydantic.fields import Field

from bilder.components.hashicorp.models import (
    FlexibleBaseModel,
    HashicorpConfig,
    HashicorpProduct,
)


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


class ConsulConfig(HashicorpConfig):
    acl: Optional[ConsulACL]
    addresses: Optional[ConsulAddresses]
    bootstrap_expect: Optional[int] = 3
    client_addr: Optional[str]
    data_dir: Optional[Path] = Path("/var/lib/consul/")
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


class Consul(HashicorpProduct):
    _name: str = "consul"
    version: str = "1.9.4"
    configuration: Dict[Path, ConsulConfig] = {
        Path("/etc/consul.d/00-default.json"): ConsulConfig()
    }
    configuration_directory: Path = Path("/etc/consul.d/")
    systemd_execution_type: str = "notify"

    @property
    def systemd_template_context(self):
        return self

    def render_configuration_files(self) -> Iterable[Tuple[Path, str]]:
        for fpath, config in self.configuration.items():  # noqa: WPS526
            yield fpath, config.json(exclude_none=True)

    @property
    def data_directory(self) -> Path:
        data_dir = list(
            filter(
                None, map(lambda config: config.data_dir, self.configuration.values())
            )
        )[0]
        return data_dir or Path("/var/lib/consul/")
