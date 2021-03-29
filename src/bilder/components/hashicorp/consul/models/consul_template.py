from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from bilder.components.hashicorp.models import HashicorpConfig, HashicorpProduct


class ConsulTemplateConfig(HashicorpConfig):
    vault_agent_token_file: Optional[Path]

    class Config:  # noqa: WPS431
        env_prefix = "consul_template_"


class ConsulTemplate(HashicorpProduct):
    _name: str = "consul-template"
    version: str = "0.25.2"
    configuration: Dict[Path, ConsulTemplateConfig] = {
        Path("/etc/consul-template.d/00-default.json"): ConsulTemplateConfig()
    }
    configuration_directory: Path = Path("/etc/consul-template.d/")

    @property
    def systemd_template_context(self):
        return self

    def render_configuration_files(self) -> Iterable[Tuple[Path, str]]:
        for fpath, config in self.configuration.items():  # noqa: WPS526
            yield fpath, config.json(exclude_none=True)
