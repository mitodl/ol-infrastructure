from pathlib import Path

from pyinfra.api import deploy
from pyinfra.operations import apt, files, server, systemd

from bilder.components.caddy.models import CaddyConfig
from bilder.lib.linux_helpers import DEFAULT_DIRECTORY_MODE


@deploy("Install Caddy")
def install_caddy(caddy_config: CaddyConfig, state=None, host=None):
    if caddy_config.plugins:
        caddy_user = "caddy"
        server.user(
            name="Create system user for Caddy",
            user=caddy_user,
            system=True,
            ensure_home=False,
            state=state,
            host=host,
        )
        files.download(
            name="Download custom build of Caddy",
            dest="/usr/local/bin/caddy",
            src=caddy_config.custom_download_url(),
            mode=DEFAULT_DIRECTORY_MODE,
            state=state,
            host=host,
        )
        files.directory(
            name="Create Caddy configuration directory",
            path="/etc/caddy/",
            user=caddy_user,
            group=caddy_user,
            present=True,
            recursive=True,
            state=state,
            host=host,
        )
        files.directory(
            name="Create Caddy configuration directory",
            path=caddy_config.data_directory,
            user=caddy_user,
            group=caddy_user,
            present=True,
            recursive=True,
            state=state,
            host=host,
        )
        files.template(
            name="Create SystemD service definition for Caddy",
            dest="/usr/lib/systemd/system/caddy.service",
            src=Path(__file__).parent.joinpath("templates/caddy.service.j2"),
            state=state,
            host=host,
        )
    else:
        apt.key(
            name="Add Caddy repository GPG key",
            src="https://dl.cloudsmith.io/public/caddy/stable/gpg.key",
            state=state,
            host=host,
        )
        apt.repo(
            name="Set up Caddy APT repository",
            src="deb https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main",  # noqa: E501
            present=True,
            filename="caddy.list",
            state=state,
            host=host,
        )
        caddy_install = apt.packages(
            name="Install Caddy from APT",
            packages=["caddy"],
            present=True,
            latest=True,
            update=True,
            state=state,
            host=host,
        )
    if caddy_config.log_file:
        files.directory(
            name="Crate Caddy log directory",
            path=caddy_config.log_file.parent,
            user=caddy_user,
            present=True,
            state=state,
            host=host,
        )
    return caddy_install.changed


@deploy("Configure Caddy")
def configure_caddy(caddy_config: CaddyConfig, state=None, host=None):
    if caddy_config.caddyfile.suffix == ".j2":
        caddy_file = files.template(
            name="Create Caddyfile",
            src=caddy_config.caddyfile,
            dest="/etc/caddy/Caddyfile",
            context=caddy_config.template_context,
            state=state,
            host=host,
        )
    else:
        caddy_file = files.put(
            name="Upload Caddyfile",
            src=caddy_config.caddyfile,
            dest="/etc/caddy/Caddyfile",
            state=state,
            host=host,
        )
    return caddy_file.changed


@deploy("Manage Caddy Service")
def caddy_service(
    caddy_config: CaddyConfig, state=None, host=None, do_restart=False, do_reload=False
):
    systemd.service(
        name="Enable Caddy service",
        service="caddy",
        running=True,
        enabled=True,
        restarted=do_restart,
        reloaded=do_reload,
        daemon_reload=caddy_config.plugins is not None,
        state=state,
        host=host,
    )
