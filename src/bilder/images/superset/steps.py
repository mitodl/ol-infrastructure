from pathlib import Path

from bilder.components.superset.models import SupersetConfig
from pyinfra.api import deploy
from pyinfra.operations import files, server, systemd


@deploy("Install superset")
def install_superset(state=None, host=None):
    server.shell(name="Sample operation", commands=["echo 'foobar'"])


@deploy("Configure superset")
def configure_superset(superset_config: SupersetConfig, state=None, host=None):
    files.template(
        name="Write a configuration file",
        src=Path(__file__).parent.joinpath("templates/conf.ini.j2"),
        dest="/etc/superset/superset.conf",
        settings=superset,
    )


@deploy("Manage superset service")
def superset_service(
    superset_config: SupersetConfig,
    state=None,
    host=None,
    do_restart=False,
    do_reload=False,
):
    systemd.service(
        name="Enable superset service",
        service="superset",
        running=True,
        enabled=True,
        restarted=do_restart,
        reloaded=do_reload,
        state=state,
        host=host,
    )
