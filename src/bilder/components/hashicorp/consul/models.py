import abc
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, Optional

from pydantic import SerializeAsAny
from pydantic.fields import Field
from pydantic_settings import SettingsConfigDict

from bilder.components.hashicorp.models import (
    FlexibleBaseModel,
    HashicorpConfig,
    HashicorpProduct,
)


class ConsulACLToken(FlexibleBaseModel):
    master: str


class ConsulACL(FlexibleBaseModel):
    tokens: Optional[list[ConsulACLToken]] = None


class ConsulAddresses(FlexibleBaseModel):
    dns: Optional[str] = "0.0.0.0"  # noqa: S104
    http: Optional[str] = "0.0.0.0"  # noqa: S104
    https: Optional[str] = None
    grpc: Optional[str] = None


class ConsulDNSConfig(FlexibleBaseModel):
    allow_stale: bool = True
    node_ttl: str = "30s"
    service_ttl: dict[str, str] = {"*": "30s"}  # noqa: RUF012


class ConsulServiceCheck(FlexibleBaseModel, abc.ABC):
    id: Optional[str] = None


class ConsulServiceTCPCheck(ConsulServiceCheck):
    id: Optional[str] = None
    name: str
    tcp: str
    interval: Optional[str] = None
    timeout: Optional[str] = None


class ConsulService(FlexibleBaseModel):
    id: Optional[str] = None
    tags: Optional[list[str]] = None
    meta: Optional[dict[str, str]] = None
    name: str
    port: Optional[int] = None
    address: Optional[str] = None
    check: Optional[SerializeAsAny[ConsulServiceCheck]] = None


class ConsulTelemetry(FlexibleBaseModel):
    dogstatsd_addr: Optional[str] = None
    disable_hostname: Optional[bool] = True
    prometheus_retention_time: Optional[str] = "60s"


class ConsulLimitConfig(FlexibleBaseModel):
    mode: Literal["disabled", "permissive", "enforcing"] = "disabled"
    read_rate: int = -1
    write_rate: int = -1


class ConsulConfig(HashicorpConfig):
    model_config = SettingsConfigDict(env_prefix="consul_")
    acl: Optional[ConsulACL] = None
    addresses: Optional[ConsulAddresses] = ConsulAddresses()
    advertise_addr: Optional[str] = None
    bootstrap_expect: Optional[int] = None
    client_addr: Optional[str] = None
    data_dir: Optional[Path] = Path("/var/lib/consul/")
    datacenter: Optional[str] = None
    disable_host_node_id: Optional[bool] = True
    dns_config: Optional[ConsulDNSConfig] = ConsulDNSConfig()
    encrypt: Optional[str] = None
    enable_syslog: bool = True
    leave_on_terminate: bool = True
    log_level: Optional[str] = "WARN"
    log_json: bool = True
    primary_datacenter: Optional[str] = None
    recursors: Optional[list[str]] = Field(
        None,
        description="List of DNS servers to use for resolving non-consul addresses",
    )
    rejoin_after_leave: bool = True
    request_limits: Optional[ConsulLimitConfig] = ConsulLimitConfig()
    retry_join: Optional[list[str]] = None
    retry_join_wan: Optional[list[str]] = None
    server: bool = False
    service: Optional[ConsulService] = None
    services: Optional[list[ConsulService]] = None
    skip_leave_on_interrupt: bool = True
    telemetry: Optional[ConsulTelemetry] = None
    ui: bool = False


class Consul(HashicorpProduct):
    _name: str = "consul"
    version: str = "1.10.0"
    configuration: dict[Path, ConsulConfig] = {  # noqa: RUF012
        Path("00-default.json"): ConsulConfig()
    }  # noqa: RUF012, RUF100
    configuration_directory: Path = Path("/etc/consul.d/")
    systemd_execution_type: str = "notify"

    @property
    def systemd_template_context(self):
        return self

    def render_configuration_files(self) -> Iterable[tuple[Path, str]]:
        for fpath, config in self.configuration.items():
            yield (
                self.configuration_directory.joinpath(fpath),
                config.model_dump_json(exclude_none=True, indent=2, by_alias=True),
            )

    @property
    def data_directory(self) -> Path:
        data_dir = next(
            iter(
                filter(
                    None,
                    (config.data_dir for config in self.configuration.values()),
                )
            )
        )
        return data_dir or Path("/var/lib/consul/")
