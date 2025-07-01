from pathlib import Path

from pydantic_settings import SettingsConfigDict

from bilder.components.traefik.models import traefik_file_provider, traefik_static
from bilder.lib.model_helpers import OLBaseSettings


class TraefikConfig(OLBaseSettings):
    model_config = SettingsConfigDict(env_prefix="traefik_")
    version: str = "2.9.1"
    user: str = "traefik"
    group: str = "traefik"
    configuration_directory: Path = Path("/etc/traefik")
    static_configuration_file: Path = Path("traefik.yaml")
    static_configuration: traefik_static.TraefikStaticConfig
    file_configurations: dict[Path, traefik_file_provider.TraefikFileConfig] | None = (
        None
    )
