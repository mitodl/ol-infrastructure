import abc
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

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
    tokens: list[ConsulACLToken] | None = None


class ConsulAddresses(FlexibleBaseModel):
    dns: str | None = "0.0.0.0"  # noqa: S104
    http: str | None = "0.0.0.0"  # noqa: S104
    https: str | None = None
    grpc: str | None = None


class ConsulDNSConfig(FlexibleBaseModel):
    allow_stale: bool = True
    node_ttl: str = "30s"
    service_ttl: dict[str, str] = {"*": "30s"}  # noqa: RUF012


class ConsulServiceCheck(FlexibleBaseModel, abc.ABC):
    id: str | None = None


class ConsulServiceTCPCheck(ConsulServiceCheck):
    id: str | None = None
    name: str
    tcp: str
    interval: str | None = None
    timeout: str | None = None


class ConsulService(FlexibleBaseModel):
    id: str | None = None
    tags: list[str] | None = None
    meta: dict[str, str] | None = None
    name: str
    port: int | None = None
    address: str | None = None
    check: SerializeAsAny[ConsulServiceCheck] | None = None


class ConsulTelemetry(FlexibleBaseModel):
    dogstatsd_addr: str | None = None
    disable_hostname: bool | None = True
    prometheus_retention_time: str | None = "60s"


class ConsulRequestLimitConfig(FlexibleBaseModel):
    mode: Literal["disabled", "permissive", "enforcing"] = "disabled"
    read_rate: int = -1
    write_rate: int = -1


class ConsulLimitConfig(FlexibleBaseModel):
    http_max_conns_per_client: int = 200
    https_handshake_timeout: str | None = "5s"
    request_limits: ConsulRequestLimitConfig = ConsulRequestLimitConfig()
    rpc_handshake_timeout: str | None = "5s"
    rpc_client_timeout: str | None = "5s"
    rpc_max_conns_per_client: int | None = None
    rpc_rate: int | None = None
    rpc_max_burst: int | None = None
    kv_max_value_size: int | None = None
    txn_max_req_len: int | None = None


class ConsulConfig(HashicorpConfig):
    model_config = SettingsConfigDict(env_prefix="consul_")
    acl: ConsulACL | None = None
    addresses: ConsulAddresses | None = ConsulAddresses()
    advertise_addr: str | None = None
    bootstrap_expect: int | None = None
    client_addr: str | None = None
    data_dir: Path | None = Path("/var/lib/consul/")
    datacenter: str | None = None
    disable_host_node_id: bool | None = True
    dns_config: ConsulDNSConfig | None = ConsulDNSConfig()
    encrypt: str | None = None
    enable_syslog: bool = True
    leave_on_terminate: bool = True
    log_level: str | None = "WARN"
    log_json: bool = True
    primary_datacenter: str | None = None
    recursors: list[str] | None = Field(
        None,
        description="List of DNS servers to use for resolving non-consul addresses",
    )
    rejoin_after_leave: bool = True
    limits: ConsulLimitConfig | None = ConsulLimitConfig()
    retry_join: list[str] | None = None
    retry_join_wan: list[str] | None = None
    server: bool = False
    service: ConsulService | None = None
    services: list[ConsulService] | None = None
    skip_leave_on_interrupt: bool = True
    telemetry: ConsulTelemetry | None = None
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
