from typing import Union

import httpx
from pyinfra import host
from pyinfra.api import deploy
from pyinfra.operations import files, server

from ol_configuration_management.components.concourse.models import (
    ConcourseBaseConfig,
    ConcourseWebConfig,
    ConcourseWorkerConfig,
)


@deploy("Install Concourse")
def install_concourse(concourse_config: ConcourseBaseConfig):
    # Create a Concourse system user
    server.user(
        name="Create the Concourse system user",
        user=concourse_config.user,
        present=True,
        home=concourse_config.deploy_directory,
        ensure_home=True,
        shell="/bin/false",
        system=True,
    )
    # Create Concourse directory under /opt to hold application environment
    files.directory(
        name="Create Concourse application directory",
        path=concourse_config.deploy_directory,
        present=True,
        user=concourse_config.user,
        recursive=True,
    )
    # Download latest Concourse release from GitHub
    concourse_archive = f"https://github.com/concourse/concourse/releases/download/v{concourse_config.version}/concourse-{concourse_config.version}-linux-amd64.tgz"
    concourse_archive_hash = f"https://github.com/concourse/concourse/releases/download/v{concourse_config.version}/concourse-{concourse_config.version}-linux-amd64.tgz.sha1"
    concourse_archive_path = f"/tmp/concourse-{concourse_config.version}.tgz"
    files.download(
        name="Download the Concourse release archive",
        src=concourse_archive,
        dest=concourse_archive_path,
        sha1sum=httpx.get(concourse_archive_hash).read().decode("utf8").split()[0],
    )
    # Unpack Concourse to /opt/concourse
    server.shell(
        name="Extract the Concourse release archive.",
        commands=[
            f"tar -xvzf {concourse_archive_path}",
            f"mv concourse {concourse_config.deploy_directory}",
        ],
    )
    # Verify ownership of Concourse directory
    files.directory(
        name="Set ownership of Concourse directory",
        path=concourse_config.deploy_directory,
        user=concourse_config.user,
    )
    # create configuration directory
    files.directory(
        name="Create Concourse configuration directory",
        path=concourse_config.config_directory,
        user=concourse_config.user,
        preset=True,
        recurseive=True,
    )


@deploy("Configure Concourse")
def configure_concourse(
    concourse_config: Union[ConcourseWebConfig, ConcourseWorkerConfig]
):
    files.template(
        name="Create Concourse environment file",
        src="templates/env_file.j2",
        dest=concourse_config.env_file_path,
        concourse_config=concourse_config,
        user=concourse_config.user,
    )
    files.template(
        name="Populate authorized_keys file",
        src="templates/authorized_keys.j2",
        dest=concourse_config.authorized_keys_file,
    )


@deploy("Register and enable Concourse service")
def concourse_service(concourse_config: ConcourseBaseConfig):
    # Create Systemd unit to manage Concourse service
    # Enable Systemd service and ensure it is running
    pass
