from pathlib import Path

from pyinfra.api import deploy
from pyinfra.operations import apt, files, server, systemd

from bilder.components.caddy.models import CaddyConfig
from bilder.lib.linux_helpers import DEFAULT_DIRECTORY_MODE


@deploy("Install Caddy")
def install_caddy(caddy_config: CaddyConfig):
    if caddy_config.plugins:
        server.user(
            name="Create system user for Caddy",
            user=caddy_config.caddy_user,
            system=True,
            ensure_home=False,
        )
        caddy_install = files.download(
            name="Download custom build of Caddy",
            dest="/usr/local/bin/caddy",
            src=caddy_config.custom_download_url(),
            mode=DEFAULT_DIRECTORY_MODE,
        )
        files.directory(
            name="Create Caddy configuration directory",
            path="/etc/caddy/",
            user=caddy_config.caddy_user,
            group=caddy_config.caddy_user,
            present=True,
            recursive=True,
        )
        files.directory(
            name="Create Caddy data directory",
            path=str(caddy_config.data_directory),
            user=caddy_config.caddy_user,
            group=caddy_config.caddy_user,
            present=True,
            recursive=True,
        )
        files.template(
            name="Create SystemD service definition for Caddy",
            dest="/usr/lib/systemd/system/caddy.service",
            src=str(
                Path(__file__)
                .resolve()
                .parent.joinpath("templates", "caddy.service.j2")
            ),
        )
    else:
        apt.packages(
            name="Install gnupg for adding Caddy repository",
            packages=["gnupg"],
        )
        apt.key(
            name="Add Caddy repository GPG key",
            src="https://dl.cloudsmith.io/public/caddy/stable/gpg.key",
        )
        apt.repo(
            name="Set up Caddy APT repository",
            src=(
                "deb https://dl.cloudsmith.io/public/caddy/stable/deb/debian"
                " any-version main"
            ),
            present=True,
            filename="caddy.list",
        )
        caddy_install = apt.packages(
            name="Install Caddy from APT",
            packages=["caddy"],
            present=True,
            latest=True,
            update=True,
        )
    files.put(
        name="Configure systemd to load environment variables from file",
        dest="/etc/systemd/system/caddy.service.d/load_env.conf",
        src=str(
            Path(__file__)
            .resolve()
            .parent.joinpath("templates", "caddy.service.override")
        ),
    )
    if caddy_config.log_file:
        files.directory(
            name="Crate Caddy log directory",
            path=str(caddy_config.log_file.parent),
            user=caddy_config.caddy_user,
            present=True,
        )
    return caddy_install.changed


@deploy("Configure Caddy")
def configure_caddy(caddy_config: CaddyConfig):
    if caddy_config.caddyfile.suffix == ".j2":
        caddy_file = files.template(
            name="Create Caddyfile",
            src=str(caddy_config.caddyfile),
            dest="/etc/caddy/Caddyfile",
            context=caddy_config.template_context,
        )
    else:
        caddy_file = files.put(
            name="Upload Caddyfile",
            src=str(caddy_config.caddyfile),
            dest="/etc/caddy/Caddyfile",
        )
    return caddy_file.changed


@deploy("Create placeholder TLS key and cert.")
def create_placeholder_tls_config(caddy_config: CaddyConfig):
    apt.packages(
        name="Install ACL package for more granular file permissions",
        packages=["acl"],
    )
    server.shell(
        name=f"Generate TLS certificate + key",  # noqa: F541
        commands=[
            (
                "openssl req -newkey rsa:4098 -x509 -sha256 -days 365 -nodes -out"
                f" {caddy_config.tls_cert_path} -keyout"
                f" {caddy_config.tls_key_path} -subj"
                ' "/C=US/ST=Massachusetts/L=Cambridge/O=MIT/OU=ODL/CN=this.is.not.a.valid.certificate.odl.mit.edu"'  # noqa: E501
            ),
        ],
    )
    # This is a workaround for a bahavior quirk of vault-template when it is running as a non-root user.  # noqa: E501
    # A simple chown caddy:caddy /etc/caddy/odl* will not suffice here.
    #
    # vault-template doesn't actually modify the existing file, but rather creates a new one and attempts to  # noqa: E501
    # overwrite the existing file and then restore the ownerships. When it runs as non-root, it cannot chown  # noqa: E501
    # so it fails. The new cert files end up with vault:vault and 660 permissions so caddy cannot open them.  # noqa: E501
    #
    # After much futzing about with setfacl, chmod, chgrp, etc ... the easiest thing is to just add vault  # noqa: E501
    # as a secondary group to the caddy user and set the ownership on the TLS files to vault:vault  # noqa: E501
    server.shell(
        name=f"Set ownership of the TLS cert and key",  # noqa: F541
        commands=[
            f"chown caddy:vault {caddy_config.tls_cert_path}",
            f"chown caddy:vault {caddy_config.tls_key_path}",
            f"chmod 0660 {caddy_config.tls_cert_path} {caddy_config.tls_key_path}",
        ],
    )
    server.shell(
        name=f"Add vault group to caddy user",  # noqa: F541
        commands=[
            f"usermod -a -G vault {caddy_config.caddy_user}",
        ],
    )


@deploy("Manage Caddy Service")
def caddy_service(
    caddy_config: CaddyConfig,
    do_restart=False,  # noqa: FBT002
    do_reload=False,  # noqa: FBT002
):  # noqa: FBT002, RUF100
    systemd.service(
        name="Enable Caddy service",
        service="caddy",
        running=True,
        enabled=True,
        restarted=do_restart,
        reloaded=do_reload,
        daemon_reload=caddy_config.plugins is not None,
    )
