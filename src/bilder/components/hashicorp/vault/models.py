import abc
from pathlib import Path
from typing import List, Optional, Union

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
    path: Path = Path("/tmp/vault_agent_token")  # noqa: S108
    mode: Optional[int]


class VaultAutoAuthAppRole(VaultAutoAuthMethodConfig):
    role_id_file_path: Path
    secret_id_file_path: Optional[Path]
    remove_secret_id_file_after_reading: bool = True
    secret_id_response_wrapping_path: Optional[Path]


class VaultAutoAuthMethod(FlexibleBaseModel):
    type: str
    mount_path: Optional[Path]
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
    config: VaultAutoAuthSinkConfig


class VaultAgentCache(FlexibleBaseModel):
    use_auto_auth_token: bool = True


class VaultSealConfig(FlexibleBaseModel, abc.ABC):
    pass  # noqa: WPS420, WPS604


class VaultStorageBackend(FlexibleBaseModel, abc.ABC):
    pass  # noqa: WPS420, WPS604


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


class VaultAutoAuthConfig(FlexibleBaseModel):
    method: VaultAutoAuthMethod
    sinks: Optional[List[VaultAutoAuthSink]]


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
    storage: VaultStorageBackend
    ha_storage: Optional[VaultStorageBackend]
    listener: Optional[List[VaultListener]]
    seal: Optional[VaultSealConfig]
    cluster_name: Optional[str]
    cache_size: Optional[str]
    disable_cache: bool = False
    disable_mlock: bool = False
    api_addr: Optional[str]
    cluster_addr: Optional[str]
    disable_clustering: bool = False
    plugin_directory: Optional[Path]
    telemetry: Optional[VaultTelemetryConfig]
    max_lease_ttl: Optional[str]
    default_lease_ttl: Optional[str]
    ui: Optional[bool] = False
    log_format: str = "json"
    log_level: str = "Warn"
    default_max_request_duration: Optional[str]

    class Config:  # noqa: WPS431
        env_prefix = "vault_"


class Vault(HashicorpProduct):
    name: str = "vault"
    version: str = "1.6.3"
    configuration: HashicorpConfig
    configuration_file: Path = Path("/etc/vault/vault.json")

    @property
    def systemd_template_context(self):
        mode_map = {VaultAgentConfig: "agent", VaultServerConfig: "server"}
        return {
            "mode": mode_map(type(self.configuration)),
            "configuration_file": self.configuration_file,
        }
