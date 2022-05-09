from pathlib import Path

from pyinfra.api import deploy
from pyinfra.operations import files, server, systemd

from bilder.components.tika.models import TikaConfig
from bilder.lib.linux_helpers import DEFAULT_DIRECTORY_MODE


@deploy("Install Tika")
def install_tika(tika_config: TikaConfig):
    server.user(
        name="Create system user for Tika",
        user=tika_config.tika_user,
        system=True,
        ensure_home=False,
    )
    server.packages(
        name="Install JDK",
        packages=["default-jdk-headless"],
        present=True,
    )
    files.directory(
        name="Create Tika installation directory.",
        path=tika_config.install_directory,
        user=tika_config.tika_user,
        group=tika_config.tika_user,
        present=True,
        recursive=True,
    )
    tika_download = files.download(
        name="Download Tika Server jar file",
        dest=str(tika_config.install_directory)
        + f"/tika-server.{tika_config.version}.jar",
        src=str(tika_config.download_url),
        mode=DEFAULT_DIRECTORY_MODE,
    )
    files.directory(
        name="Create Tika log directory.",
        path="/var/log/tika",
        user=tika_config.tika_user,
        group=tika_config.tika_user,
        present=True,
        recursive=True,
    )


@deploy("Configure Tika")
def configure_tika(tika_config: TikaConfig):
    tika_config_file = files.template(
        name="Create tika-config.xml file",
        src=str(
            Path(__file__).resolve().parent.joinpath("templates", "tika-config.xml")
        ),
        dest=tika_config.template_context["tika_config_file"],
        context=tika_config.template_context,
    )
    log_config_file = files.put(
        name="Create log4j configuration file",
        src=str(Path(__file__).resolve().parent.joinpath("files", "log4j2_tika.xml")),
        dest=tika_config.template_context["tika_log_config_file"],
    )
    service_defintion = files.template(
        name="Create Tika service definition",
        dest="/usr/lib/systemd/system/tika-server.service",
        src=str(
            Path(__file__)
            .resolve()
            .parent.joinpath("templates", "tika-server.service.j2")
        ),
        context=tika_config.template_context,
    )


@deploy("Manage Tika Service")
def tika_service(tika_config: TikaConfig):
    systemd.service(
        name="Enable Tika service",
        service="tika-server",
        running=True,
        enabled=True,
        daemon_reload=True,
    )


# @deploy("Configure Caddy")
# def configure_caddy(caddy_config: CaddyConfig):
#    if caddy_config.caddyfile.suffix == ".j2":
#        caddy_file = files.template(
#            name="Create Caddyfile",
#            src=str(caddy_config.caddyfile),
#            dest="/etc/caddy/Caddyfile",
#            context=caddy_config.template_context,
#        )
#    else:
#        caddy_file = files.put(
#            name="Upload Caddyfile",
#            src=str(caddy_config.caddyfile),
#            dest="/etc/caddy/Caddyfile",
#        )
#    return caddy_file.changed
#
#
# @deploy("Manage Caddy Service")
# def caddy_service(caddy_config: CaddyConfig, do_restart=False, do_reload=False):
#    systemd.service(
#        name="Enable Caddy service",
#        service="caddy",
#        running=True,
#        enabled=True,
#        restarted=do_restart,
#        reloaded=do_reload,
#        daemon_reload=caddy_config.plugins is not None,
#    )
