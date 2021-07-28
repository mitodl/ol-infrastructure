import abc
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, Union

from pydantic.types import conint

from bilder.components.hashicorp.models import (
    FlexibleBaseModel,
    HashicorpConfig,
    HashicorpProduct,
)


class VaultAutoAuthMethodConfig(FlexibleBaseModel, abc.ABC):
    pass  # noqa: WPS420, WPS604


class VaultAutoAuthSinkConfig(FlexibleBaseModel, abc.ABC):
    pass  # noqa: WPS420, WPS604


class VaultAutoAuthFileSink(VaultAutoAuthSinkConfig):
    path: Path = Path("/etc/vault/vault_agent_token")  # noqa: S108
    mode: Optional[int]


class VaultAutoAuthAppRole(VaultAutoAuthMethodConfig):
    role_id_file_path: Path
    secret_id_file_path: Optional[Path]
    remove_secret_id_file_after_reading: bool = True
    secret_id_response_wrapping_path: Optional[Path]


class VaultAutoAuthAWS(VaultAutoAuthMethodConfig):
    # The type of authentication; must be ec2 or iam.
    type: str = "iam"
    # The role to authenticate against on Vault.
    role: str
    # In seconds, how frequently the Vault agent should check for new credentials if
    # using the iam type.
    credential_poll_interval: Optional[int]
    access_key: Optional[str]
    secret_key: Optional[str]
    region: str = "us-east-1"
    session_token: Optional[str]
    header_value: Optional[str]


class VaultAutoAuthMethod(FlexibleBaseModel):
    type: str
    mount_path: Optional[str]
    namespace: Optional[str]
    wrap_ttl: Optional[Union[str, int]]
    config: VaultAutoAuthMethodConfig


class VaultAutoAuthSink(FlexibleBaseModel):
    type: str
    wrap_ttl: Optional[Union[str, int]]
    dh_type: Optional[str]
    dh_path: Optional[Path]
    derive_key: bool = False
    aad: Optional[str]
    aad_env_var: Optional[str]
    config: List[VaultAutoAuthSinkConfig]


class VaultAgentCache(FlexibleBaseModel):
    use_auto_auth_token: Union[str, bool] = True


class VaultSealConfig(FlexibleBaseModel, abc.ABC):
    pass  # noqa: WPS420, WPS604


class VaultStorageBackend(FlexibleBaseModel, abc.ABC):
    pass  # noqa: WPS420, WPS604


class VaultAwsKmsSealConfig(VaultSealConfig):
    region: Optional[str] = "us-east-1"
    access_key: Optional[str]
    secret_key: Optional[str]
    kms_key_id: str


class VaultTelemetryConfig(FlexibleBaseModel):
    usage_gauge_period: Optional[str]
    maximum_gauge_cardinality: Optional[int]
    disable_hostname: bool = False
    enable_hostname_label: bool = False


class ConsulStorageBackend(VaultStorageBackend):
    address: Optional[str]
    check_timeout: Optional[str] = "5s"
    consistency_mode: Optional[str] = "default"
    disable_registration: bool = False
    max_parallel: Optional[str]
    path: Optional[str] = "vault/"
    scheme: Optional[str] = "http"
    service: Optional[str] = "vault"
    service_tags: Optional[str]
    token: Optional[str]
    session_ttl: Optional[str] = "15s"
    tls_ca_file: Optional[Path]
    tls_cert_file: Optional[Path]
    tls_key_file: Optional[Path]
    tls_min_version: Optional[str] = "tls12"


class IntegratedStorageBackend(VaultStorageBackend):
    path: Path
    performance_multiplier: conint(ge=0, le=10) = 0  # type: ignore


class VaultAutoAuthConfig(FlexibleBaseModel):
    method: VaultAutoAuthMethod
    sink: Optional[List[VaultAutoAuthSink]]


class VaultConnectionConfig(FlexibleBaseModel):
    address: str
    ca_cert: Optional[Path]
    ca_path: Optional[Path]
    client_cert: Optional[Path]
    client_key: Optional[Path]
    tls_skip_verify: bool = False
    tls_server_name: Optional[str]


class VaultTemplate(FlexibleBaseModel):
    source: Optional[Path]
    contents: Optional[str]  # noqa: WPS110
    destination: Path
    create_dest_dirs: bool = True
    command: Optional[str]


class VaultListener(FlexibleBaseModel):
    type: str  # one of "tcp" or "unix"
    address: str
    tls_disable: bool = False
    tls_key_file: Optional[Path]
    tls_cert_file: Optional[Path]


class VaultAgentConfig(HashicorpConfig):
    vault: Optional[VaultConnectionConfig]
    auto_auth: Optional[VaultAutoAuthConfig]
    cache: Optional[VaultAgentCache] = VaultAgentCache()
    pid_file: Optional[Path]
    exit_after_auth: bool = False
    template: Optional[List[VaultTemplate]]
    listener: Optional[List[VaultListener]]

    class Config:  # noqa: WPS431
        env_prefix = "vault_agent_"


class VaultServerConfig(HashicorpConfig):
    api_addr: Optional[str]
    cache_size: Optional[str]
    cluster_addr: Optional[str]
    cluster_name: Optional[str]
    default_lease_ttl: Optional[str]
    default_max_request_duration: Optional[str]
    disable_cache: bool = False
    disable_clustering: bool = False
    disable_mlock: bool = False
    ha_storage: Optional[VaultStorageBackend]
    listener: Optional[List[VaultListener]]
    log_format: str = "json"
    log_level: str = "Warn"
    max_lease_ttl: Optional[str]
    plugin_directory: Optional[Path]
    seal: Optional[VaultSealConfig]
    storage: VaultStorageBackend
    telemetry: Optional[VaultTelemetryConfig]
    ui: Optional[bool] = False

    class Config:  # noqa: WPS431
        env_prefix = "vault_"


class Vault(HashicorpProduct):
    _name: str = "vault"
    version: str = "1.6.3"
    configuration: HashicorpConfig = VaultAgentConfig()
    configuration_file: Path = Path("/etc/vault/vault.json")

    @property
    def systemd_template_context(self):
        mode_map = {VaultAgentConfig: "agent", VaultServerConfig: "server"}
        return {
            "mode": mode_map[type(self.configuration)],
            "configuration_file": self.configuration_file,
        }

    def render_configuration_files(self) -> Iterable[Tuple[Path, str]]:
        return [
            (
                self.configuration_file,
                self.configuration.json(exclude_none=True, indent=2),
            )
        ]
