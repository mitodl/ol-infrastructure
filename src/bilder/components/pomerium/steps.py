from pathlib import Path

from pyinfra.api import deploy
from pyinfra.operations import files

from bilder.components.pomerium.models import PomeriumConfig


@deploy("Configure Pomerium")
def configure_pomerium(pomerium_config: PomeriumConfig):
    files.directory(
        name="Create Pomerium configuration directory.",
        path=pomerium_config.configuration_directory,
        user="root",
        group="root",
        present=True,
    )
    pomerium_docker_compose_file = files.template(  # noqa: F841
        name="Create the docker compose file for pomerium",
        src=str(
            Path(__file__)
            .resolve()
            .parent.joinpath("templates", "compose-pomerium.yaml.j2")
        ),
        dest=str(pomerium_config.docker_compose_file),
        context=pomerium_config,
    )
    vault_template_directory = files.directory(  # noqa: F841
        name="Create the vault templates directory if it doesn't already exist.",
        path=pomerium_config.configuration_template_directory,
        user="vault",
        group="vault",
        present=True,
    )
    files.put(
        name="Create the pomerium configuration template file",
        src=str(Path(__file__).resolve().parent.joinpath("files", "config.yaml.tmpl")),
        dest=str(pomerium_config.configuration_template_file),
    )
