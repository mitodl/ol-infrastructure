from pathlib import Path
from typing import Union

import httpx
from pyinfra.api import deploy
from pyinfra.operations import files, server, systemd

from ol_configuration_management.components.concourse.models import (
    ConcourseBaseConfig,
    ConcourseWebConfig,
    ConcourseWorkerConfig,
)
from ol_configuration_management.facts import has_systemd  # noqa: F401


@deploy("Install Concourse")
def install_concourse(
    concourse_config: ConcourseBaseConfig, sudo=True, state=None, host=None
):
    # Create a Concourse system user
    server.user(
        name="Create the Concourse system user",
        user=concourse_config.user,
        present=True,
        home=concourse_config.deploy_directory,
        ensure_home=True,
        shell="/bin/false",  # noqa: S604
        system=True,
        state=state,
        host=host,
        sudo=sudo,
    )
    # Create Concourse directory under /opt to hold application environment
    files.directory(
        name="Create Concourse application directory",
        path=concourse_config.deploy_directory,
        present=True,
        user=concourse_config.user,
        recursive=True,
        state=state,
        host=host,
        sudo=sudo,
    )
    # Download latest Concourse release from GitHub
    concourse_archive = f"https://github.com/concourse/concourse/releases/download/v{concourse_config.version}/concourse-{concourse_config.version}-linux-amd64.tgz"
    concourse_archive_hash = f"https://github.com/concourse/concourse/releases/download/v{concourse_config.version}/concourse-{concourse_config.version}-linux-amd64.tgz.sha1"
    concourse_archive_path = (
        f"/tmp/concourse-{concourse_config.version}.tgz"  # noqa: S108
    )
    files.download(
        name="Download the Concourse release archive",
        src=concourse_archive,
        dest=concourse_archive_path,
        sha1sum=httpx.get(concourse_archive_hash).read().decode("utf8").split()[0],
        state=state,
        host=host,
        sudo=sudo,
    )
    # Unpack Concourse to /opt/concourse
    server.shell(
        name="Extract the Concourse release archive.",
        commands=[
            f"tar -xvzf {concourse_archive_path}",
            f"mv concourse/* {concourse_config.deploy_directory}",
        ],
        state=state,
        host=host,
        sudo=sudo,
    )
    # Verify ownership of Concourse directory
    files.directory(
        name="Set ownership of Concourse directory",
        path=concourse_config.deploy_directory,
        user=concourse_config.user,
        state=state,
        host=host,
        sudo=sudo,
    )
    # create configuration directory
    files.directory(
        name="Create Concourse configuration directory",
        path=concourse_config.config_directory,
        user=concourse_config.user,
        present=True,
        recursive=True,
        state=state,
        host=host,
        sudo=sudo,
    )


@deploy("Configure Concourse")
def configure_concourse(
    concourse_config: Union[ConcourseWebConfig, ConcourseWorkerConfig],
    sudo=True,
    state=None,
    host=None,
):
    concourse_env_file = files.template(
        name="Create Concourse environment file",
        src=Path(__file__).parent.joinpath("templates/env_file.j2"),
        dest=concourse_config.env_file_path,
        concourse_config=concourse_config,
        user=concourse_config.user,
        state=state,
        host=host,
        sudo=sudo,
    )
    if host.fact.has_systemd:
        systemd.service(
            name="Restart the Concourse service when the configuration changes",
            service="concourse",
            restarted=concourse_env_file.changed,
            state=state,
            host=host,
            sudo=sudo,
        )
    # if concourse_config._node_type == "web"
    # Create authorized_keys file
    # Write tsa_host_key and session_signing_key
    # else
    # write worker private key


@deploy("Register and enable Concourse service")
def register_concourse_service(
    concourse_config: Union[ConcourseWebConfig, ConcourseWorkerConfig],
    sudo=True,
    state=None,
    host=None,
):
    # Create Systemd unit to manage Concourse service
    systemd_unit = files.template(
        name="Create concourse Systemd unit definition",
        src=Path(__file__).parent.joinpath("templates/concourse.service.j2"),
        dest="/etc/systemd/system/concourse.service",
        concourse_config=concourse_config,
        state=state,
        host=host,
        sudo=sudo,
    )
    # Enable Systemd service and ensure it is running
    systemd.service(
        name="Ensure Concourse service is enabled and running.",
        service="concourse",
        running=True,
        enabled=True,
        daemon_reload=systemd_unit.changed,
        state=state,
        host=host,
        sudo=sudo,
    )
