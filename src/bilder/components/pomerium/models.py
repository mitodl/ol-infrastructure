from pathlib import Path

from bilder.lib.linux_helpers import DOCKER_COMPOSE_DIRECTORY
from bilder.lib.model_helpers import OLBaseSettings


class PomeriumConfig(OLBaseSettings):

    configuration_directory: Path = Path("/etc/pomerium")
    configuration_template_directory: Path = Path("/etc/vault-templates.d")

    docker_compose_file: Path = Path(
        DOCKER_COMPOSE_DIRECTORY + "/pomerium-compose.yaml"
    )

    listener_port: str = "443"
    docker_image: str = "pomerium/pomerium"
    docker_tag: str = "latest"

    configuration_file: Path = configuration_directory.joinpath("config.yaml")
    certificate_file: Path = configuration_directory.joinpath("star.odl.mit.edu.crt")
    certificate_key_file: Path = configuration_directory.joinpath(
        "star.odl.mit.edu.key"
    )
    configuration_template_file: Path = configuration_template_directory.joinpath(
        "config.yaml.tmpl"
    )

    class Config:
        env_prefix = "pomerium_"
