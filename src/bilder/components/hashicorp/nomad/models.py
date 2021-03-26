from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from bilder.components.hashicorp.models import FlexibleBaseModel, HashicorpProduct
from bilder.lib.model_helpers import OLBaseSettings


class NomadClientConfig(FlexibleBaseModel):
    enabled: bool = True


class NomadServerConfig(FlexibleBaseModel):
    enabled: bool = True


class NomadConfig(OLBaseSettings):
    client: Optional[NomadClientConfig]
    data_dir: Optional[Path] = Path("/var/lib/nomad/")
    server: Optional[NomadServerConfig]

    class Config:  # noqa: WPS431
        env_prefix = "nomad_"


class NomadJob(OLBaseSettings):
    class Config:  # noqa: WPS431
        env_prefix = "nomad_job_"


class Nomad(HashicorpProduct):
    name: str = "nomad"
    version: str = "1.0.4"
    configuration: Dict[Path, NomadConfig] = {
        Path("/etc/nomad.d/00-default.json"): NomadConfig(client=NomadClientConfig())
    }
    configuration_directory: Path = Path("/etc/nomad.d/")

    @property
    def systemd_template_context(self):
        return self

    def render_configuration_files(self) -> Iterable[Tuple[Path, str]]:
        for fpath, config in self.configuration.items():  # noqa: WPS526
            yield fpath, config.json(exclude_none=True)

    @property
    def data_directory(self) -> Path:
        for config in self.configuration.values():
            data_dir = config.data_dir
        return data_dir or Path("/var/lib/nomad/")
