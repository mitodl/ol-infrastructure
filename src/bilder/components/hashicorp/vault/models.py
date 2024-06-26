import abc
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Optional, Self, Union

from pydantic import Field, SerializeAsAny, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from bilder.components.hashicorp.models import (
    FlexibleBaseModel,
    HashicorpConfig,
    HashicorpProduct,
)
from bilder.lib.model_helpers import parse_simple_duration_string


class VaultAutoAuthMethodConfig(FlexibleBaseModel, abc.ABC):
    pass


class VaultAutoAuthSinkConfig(FlexibleBaseModel, abc.ABC):
    pass


class VaultAutoAuthFileSink(VaultAutoAuthSinkConfig):
    path: Path = Path("/etc/vault/vault_agent_token")
    mode: Optional[int] = None


class VaultAutoAuthAppRole(VaultAutoAuthMethodConfig):
    role_id_file_path: Path
    secret_id_file_path: Optional[Path] = None
    remove_secret_id_file_after_reading: bool = True
    secret_id_response_wrapping_path: Optional[Path] = None


class VaultAutoAuthAWS(VaultAutoAuthMethodConfig):
    # The type of authentication; must be ec2 or iam.
    type: str = "iam"
    # The role to authenticate against on Vault.
    role: str
    # In seconds, how frequently the Vault agent should check for new credentials if
    # using the iam type.
    credential_poll_interval: Optional[int] = None
    access_key: Optional[str] = None
    secret_key: Optional[str] = None
    region: str = "us-east-1"
    session_token: Optional[str] = None
    header_value: Optional[str] = None


class VaultAutoAuthMethod(FlexibleBaseModel):
    type: str
    mount_path: Optional[str] = None
    namespace: Optional[str] = None
    wrap_ttl: Optional[Union[str, int]] = None
    config: SerializeAsAny[VaultAutoAuthMethodConfig]


class VaultAutoAuthSink(FlexibleBaseModel):
    type: str
    wrap_ttl: Optional[Union[str, int]] = None
    dh_type: Optional[str] = None
    dh_path: Optional[Path] = None
    derive_key: bool = False
    aad: Optional[str] = None
    aad_env_var: Optional[str] = None
    config: list[SerializeAsAny[VaultAutoAuthSinkConfig]]


class VaultAgentCache(FlexibleBaseModel):
    use_auto_auth_token: Union[str, bool] = True


class VaultAwsKmsSealConfig(FlexibleBaseModel):
    region: Optional[str] = "us-east-1"
    access_key: Optional[str] = None
    secret_key: Optional[str] = None
    kms_key_id: Optional[str] = None
    endpoint: Optional[str] = None
    session_token: Optional[str] = None


class VaultSealConfig(FlexibleBaseModel):
    awskms: Optional[VaultAwsKmsSealConfig] = None


class VaultTelemetryConfig(FlexibleBaseModel):
    usage_gauge_period: Optional[str] = None
    maximum_gauge_cardinality: Optional[int] = None
    disable_hostname: bool = False
    enable_hostname_label: bool = False


class ConsulStorageBackend(FlexibleBaseModel):
    address: Optional[str] = None
    check_timeout: Optional[str] = "5s"
    consistency_mode: Optional[str] = "default"
    disable_registration: bool = False
    max_parallel: Optional[str] = None
    path: Optional[str] = "vault/"
    scheme: Optional[str] = "http"
    service: Optional[str] = "vault"
    service_tags: Optional[str] = None
    token: Optional[str] = None
    session_ttl: Optional[str] = "15s"
    tls_ca_file: Optional[Path] = None
    tls_cert_file: Optional[Path] = None
    tls_key_file: Optional[Path] = None
    tls_min_version: Optional[str] = "tls12"


class VaultRetryJoin(FlexibleBaseModel):
    leader_api_addr: Optional[str] = None
    auto_join: Optional[str] = None
    auto_join_scheme: Optional[str] = None
    auto_join_port: Optional[int] = None
    leader_tls_servername: Optional[str] = None
    leader_ca_cert_file: Optional[Path] = None
    leader_client_cert_file: Optional[Path] = None
    leader_client_key_file: Optional[Path] = None
    leader_ca_cert: Optional[str] = None
    leader_client_cert: Optional[str] = None
    leader_client_key: Optional[str] = None


class IntegratedRaftStorageBackend(FlexibleBaseModel):
    path: Path = Path("/var/lib/vault/raft/")
    performance_multiplier: Optional[Annotated[int, Field(ge=0, le=10)]] = None
    # The node_id is an optional parameter that will receive an autogenerated UUID if
    # not set.
    # https://github.com/hashicorp/vault/blob/master/physical/raft/raft.go#L289-L329
    node_id: Optional[str] = None
    trailing_logs: Optional[int] = None
    snapshot_threshold: Optional[int] = None
    retry_join: Optional[list[VaultRetryJoin]] = None
    max_entry_size: Optional[int] = None
    autopilot_reconcile_interval: Optional[str] = None


class VaultStorageBackend(FlexibleBaseModel):
    """Container class for holding named references to storage implementations.

    In order to add support for configuring an additional storage backend, the name of
    the backend as defined by Vault is set as the attribute name, and the type of the
    attribute is Optional[<NameOfStorageClass>].  This allows us to pass an instance of
    that class object to the associated attribute so that the rendered JSON is of the
    form

    {"storage": {"raft": {"path": "/data/storage/path"}}}
    """

    consul: Optional[ConsulStorageBackend] = None
    raft: Optional[IntegratedRaftStorageBackend] = None


class VaultAutoAuthConfig(FlexibleBaseModel):
    method: VaultAutoAuthMethod
    sink: Optional[list[VaultAutoAuthSink]] = None


class VaultConnectionConfig(FlexibleBaseModel):
    address: str
    ca_cert: Optional[Path] = None
    ca_path: Optional[Path] = None
    client_cert: Optional[Path] = None
    client_key: Optional[Path] = None
    tls_skip_verify: bool = False
    tls_server_name: Optional[str] = None


class VaultTemplate(FlexibleBaseModel):
    source: Optional[Path] = None
    contents: Optional[str] = None
    destination: Path
    create_dest_dirs: bool = True
    command: Optional[str] = None


class VaultTelemetryListener(FlexibleBaseModel):
    unauthenticated_metrics_access: bool = False


class VaultTCPListener(FlexibleBaseModel):
    address: Optional[str] = None
    cluster_address: Optional[str] = None
    http_idle_timeout: Optional[str] = None
    http_read_header_timeout: Optional[str] = None
    http_read_timeout: Optional[str] = None
    http_write_timeout: Optional[str] = None
    max_request_size: Optional[int] = None
    max_request_duration: Optional[str] = None
    tls_disable: Optional[bool] = None
    tls_cert_file: Optional[Path] = None
    tls_key_file: Optional[Path] = None
    tls_min_version: Optional[str] = None
    telemetry: Optional[VaultTelemetryListener] = None


class VaultListener(FlexibleBaseModel):
    tcp: Optional[VaultTCPListener] = None


class ConsulServiceRegistration(FlexibleBaseModel):
    # address of Consul agent to communicate with
    address: Optional[str] = None
    check_timeout: Optional[str] = None
    disable_registration: str = "false"
    scheme: Optional[str] = "http"
    service: Optional[str] = "vault"
    service_tags: Optional[list[str]] = None
    service_address: Optional[str] = ""
    # Consul ACL token to authorize setting the service definition
    token: Optional[str] = None
    tls_ca_file: Optional[Path] = None
    tls_cert_file: Optional[Path] = None
    tls_key_file: Optional[Path] = None
    tls_min_version: Optional[str] = None
    tls_skip_verify: Optional[bool] = None


class VaultServiceRegistration(FlexibleBaseModel):
    consul: Optional[ConsulServiceRegistration] = None


class VaultAgentConfig(HashicorpConfig):
    model_config = SettingsConfigDict(env_prefix="vault_agent_")
    vault: Optional[VaultConnectionConfig] = None
    auto_auth: Optional[VaultAutoAuthConfig] = None
    cache: Optional[VaultAgentCache] = VaultAgentCache()
    pid_file: Optional[Path] = None
    exit_after_auth: bool = False
    template: Optional[list[VaultTemplate]] = None
    listener: Optional[list[VaultListener]] = None
    restart_period: Optional[str] = None
    restart_jitter: Optional[str] = None

    @model_validator(mode="after")
    def validate_restart_settings(self) -> Self:
        if self.restart_period is not None and self.restart_jitter is None:
            msg = "If restart_period is set then a restart_jitter must be supplied."
            raise ValueError(msg)
        return self

    @field_validator("restart_period")
    @classmethod
    def validate_restart_period(cls, restart_period) -> str:
        if restart_period and not parse_simple_duration_string(restart_period):
            msg = f"Invalid restart_period duration: {restart_period}"
            raise ValueError(msg)
        return restart_period

    @field_validator("restart_jitter")
    @classmethod
    def validate_restart_jitter(cls, restart_jitter) -> str:
        if restart_jitter and not parse_simple_duration_string(restart_jitter):
            msg = f"Invalid restart_jitter duration: {restart_jitter}"
            raise ValueError(msg)
        return restart_jitter


class VaultServerConfig(HashicorpConfig):
    model_config = SettingsConfigDict(env_prefix="vault_")
    api_addr: Optional[str] = None
    cache_size: Optional[str] = None
    cluster_addr: Optional[str] = None
    cluster_name: Optional[str] = None
    default_lease_ttl: Optional[str] = None
    default_max_request_duration: Optional[str] = None
    disable_cache: bool = False
    disable_clustering: bool = False
    disable_mlock: bool = False
    ha_storage: Optional[list[VaultStorageBackend]] = None
    listener: Optional[list[VaultListener]] = None
    log_format: str = "json"
    log_level: str = "Warn"
    max_lease_ttl: Optional[str] = None
    plugin_directory: Optional[Path] = None
    seal: Optional[list[VaultSealConfig]] = None
    service_registration: Optional[VaultServiceRegistration] = None
    # Set storage as optional to allow for splitting into a separate config file
    storage: Optional[VaultStorageBackend] = None
    telemetry: Optional[VaultTelemetryConfig] = None
    ui: Optional[bool] = False


class Vault(HashicorpProduct):
    _name: str = "vault"
    version: str = "1.8.0"
    configuration: dict[Path, HashicorpConfig] = {  # noqa: RUF012
        Path("vault.json"): VaultAgentConfig()
    }
    configuration_directory: Path = Path("/etc/vault/")
    data_directory: Path = Path("/var/lib/vault/")

    @field_validator("configuration")
    @classmethod
    def validate_consistent_config_types(cls, configuration):
        type_set = {type(config_obj) for config_obj in configuration.values()}
        if len(type_set) > 1:
            msg = "There are server and agent configuration objects present"
            raise ValueError(msg)
        return configuration

    def operating_mode(self) -> str:
        mode_map = {VaultAgentConfig: "agent", VaultServerConfig: "server"}
        return mode_map[{type(conf) for conf in self.configuration.values()}.pop()]

    @property
    def systemd_template_context(self):
        context_dict = {
            "mode": self.operating_mode(),
            "configuration_directory": self.configuration_directory,
        }
        if self.operating_mode() == "agent":
            conf_path = next(iter(self.configuration.keys()))
            context_dict["configuration_file"] = conf_path
            context_dict["restart_period"] = self.configuration[
                conf_path
            ].restart_period
            context_dict["restart_jitter"] = self.configuration[
                conf_path
            ].restart_jitter
        return context_dict

    def render_configuration_files(self) -> Iterable[tuple[Path, str]]:
        for fpath, config in self.configuration.items():
            yield (
                self.configuration_directory.joinpath(fpath),
                config.model_dump_json(
                    exclude_none=True,
                    exclude={"restart_period", "restart_jitter"},
                    indent=2,
                ),
            )
