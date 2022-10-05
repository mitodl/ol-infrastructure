from pathlib import Path

from pyinfra.api import deploy
from pyinfra.operations import files, server, systemd

from bilder.components.traefik.models import TraefikConfig


@deploy("Install traefik")
def install_traefik(state=None, host=None):
    server.shell(name="Sample operation", commands=["echo 'foobar'"])


@deploy("Configure traefik")
def configure_traefik(traefik_config: TraefikConfig, state=None, host=None):
    files.template(
        name="Write a configuration file",
        src=Path(__file__).parent.joinpath("templates/conf.ini.j2"),
        dest="/etc/traefik/traefik.conf",
        settings=traefik_config,
    )


@deploy("Manage traefik service")
def traefik_service(
    traefik_config: TraefikConfig,
    state=None,
    host=None,
    do_restart=False,
    do_reload=False,
):
    systemd.service(
        name="Enable traefik service",
        service="traefik",
        running=True,
        enabled=True,
        restarted=do_restart,
        reloaded=do_reload,
        state=state,
        host=host,
    )
