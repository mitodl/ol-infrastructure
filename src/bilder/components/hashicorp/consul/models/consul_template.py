from pathlib import Path

from bilder.components.hashicorp.models import HashicorpConfig, HashicorpProduct


class ConsulTemplateConfig(HashicorpConfig):
    class Config:  # noqa: WPS431
        env_prefix = "consul_template_"


class ConsulTemplate(HashicorpProduct):
    name: str = "consul-template"
    version: str = "0.25.2"
    configuration_directory: Path = Path("/etc/consul-template.d/")

    @property
    def systemd_template_context(self):
        return self
