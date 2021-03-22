import abc
from pathlib import Path
from typing import Any, Optional

from bilder.components.hashicorp.models import FlexibleBaseModel, HashicorpConfig


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


class VaultConfig(HashicorpConfig):
    storage: VaultStorageBackend
    ha_storage: Optional[VaultStorageBackend]
    listener: Optional[Any]
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
