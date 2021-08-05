import abc
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from pydantic.fields import Field

from bilder.components.hashicorp.models import (
    FlexibleBaseModel,
    HashicorpConfig,
    HashicorpProduct,
)


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


class ConsulServiceCheck(FlexibleBaseModel, abc.ABC):
    id: Optional[str]


class ConsulServiceTCPCheck(ConsulServiceCheck):
    id: Optional[str]
    name: str
    tcp: str
    interval: Optional[str]
    timeout: Optional[str]


class ConsulService(FlexibleBaseModel):
    id: Optional[str]
    tags: Optional[List[str]]
    meta: Optional[Dict[str, str]]
    name: str
    port: Optional[int]
    address: Optional[str]
    check: Optional[ConsulServiceCheck]


class ConsulTelemetry(FlexibleBaseModel):
    dogstatsd_addr: str = "127.0.0.1:8125"


class ConsulConfig(HashicorpConfig):
    acl: Optional[ConsulACL]
    addresses: Optional[ConsulAddresses] = ConsulAddresses()
    bootstrap_expect: Optional[int]
    client_addr: Optional[str]
    data_dir: Optional[Path] = Path("/var/lib/consul/")
    datacenter: Optional[str]
    disable_host_node_id: Optional[bool] = True
    dns_config: Optional[ConsulDNSConfig] = ConsulDNSConfig()
    encrypt: Optional[str]
    enable_syslog: bool = True
    leave_on_terminate: bool = True
    log_level: Optional[str] = "WARN"
    primary_datacenter: Optional[str]
    recursors: Optional[List[str]] = Field(
        None,
        description="List of DNS servers to use for resolving non-consul addresses",
    )
    rejoin_after_leave: bool = True
    retry_join: Optional[List[str]]
    retry_join_wan: Optional[List[str]]
    server: bool = False
    service: Optional[ConsulService]
    services: Optional[List[ConsulService]]
    skip_leave_on_interrupt: bool = True
    telemetry: Optional[ConsulTelemetry]
    ui: bool = False

    class Config:  # noqa: WPS431
        env_prefix = "consul_"


class Consul(HashicorpProduct):
    _name: str = "consul"
    version: str = "1.10.0"
    configuration: Dict[Path, ConsulConfig] = {Path("00-default.json"): ConsulConfig()}
    configuration_directory: Path = Path("/etc/consul.d/")
    systemd_execution_type: str = "notify"

    @property
    def systemd_template_context(self):
        return self

    def render_configuration_files(self) -> Iterable[Tuple[str, str]]:
        for fpath, config in self.configuration.items():  # noqa: WPS526
            yield str(self.configuration_directory.joinpath(fpath)), config.json(
                exclude_none=True, indent=2
            )

    @property
    def data_directory(self) -> Path:
        data_dir = list(
            filter(
                None, map(lambda config: config.data_dir, self.configuration.values())
            )
        )[0]
        return data_dir or Path("/var/lib/consul/")
