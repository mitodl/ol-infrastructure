import tempfile
from pathlib import Path
from typing import Union

import httpx
from pyinfra.api import deploy
from pyinfra.operations import files, server, systemd

from bilder.components.concourse.models import (
    ConcourseBaseConfig,
    ConcourseWebConfig,
    ConcourseWorkerConfig,
)
from bilder.facts import has_systemd  # noqa: F401


@deploy("Install Concourse")
def install_concourse(concourse_config: ConcourseBaseConfig, state=None, host=None):
    # Create a Concourse system user
    server.user(
        name="Create the Concourse system user",
        user=concourse_config.user,
        present=True,
        home=concourse_config.deploy_directory,
        ensure_home=False,
        shell="/bin/false",  # noqa: S604
        system=True,
        state=state,
        host=host,
    )
    installation_directory = (
        f"{concourse_config.deploy_directory}-{concourse_config.version}"
    )
    if not host.fact.directory(installation_directory):
        # Download latest Concourse release from GitHub
        concourse_archive = f"https://github.com/concourse/concourse/releases/download/v{concourse_config.version}/concourse-{concourse_config.version}-linux-amd64.tgz"  # noqa: E501
        concourse_archive_hash = f"https://github.com/concourse/concourse/releases/download/v{concourse_config.version}/concourse-{concourse_config.version}-linux-amd64.tgz.sha1"  # noqa: E501
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
        )
        # Unpack Concourse to /opt/concourse
        server.shell(
            name="Extract the Concourse release archive.",
            commands=[
                f"tar -xvzf {concourse_archive_path}",
                f"mv concourse {installation_directory}",
            ],
            state=state,
            host=host,
        )
        # Verify ownership of Concourse directory
        files.directory(
            name="Set ownership of Concourse directory",
            path=installation_directory,
            user=concourse_config.user,
            state=state,
            host=host,
        )
    # Link Concourse installation to target directory
    active_installation_path = files.link(
        name="Link Concourse installation to target directory",
        path=concourse_config.deploy_directory,
        target=f"{installation_directory}",
        user=concourse_config.user,
        symbolic=True,
        present=True,
        state=state,
        host=host,
    )
    return active_installation_path.changed


@deploy("Manage Web Node Keys")
def _manage_web_node_keys(
    concourse_config: ConcourseWebConfig,
    state=None,
    host=None,
    sudo=True,
):
    # Create authorized_keys file
    files.template(
        name="Create authorized_keys file to permit worker connections",
        src=Path(__file__).parent.joinpath("templates", "authorized_keys.j2"),
        dest=concourse_config.authorized_keys_file,
        user=concourse_config.user,
        authorized_keys=concourse_config.authorized_worker_keys or [],
        state=state,
        host=host,
        sudo=sudo,
    )
    # Write tsa_host_key and session_signing_key
    if concourse_config.tsa_host_key:
        host_key_file = tempfile.NamedTemporaryFile(delete=False)
        host_key_file.write(concourse_config.tsa_host_key.encode("utf8"))
        files.put(
            name="Write tsa_host_key file",
            dest=concourse_config.tsa_host_key_path,
            user=concourse_config.user,
            mode="600",
            src=host_key_file.name,
            state=state,
            host=host,
            sudo=sudo,
        )
    elif not host.fact.file(concourse_config.tsa_host_key_path):
        server.shell(
            name="Generate a tsa host key",
            commands=[
                f"{concourse_config.deploy_directory}/bin/concourse generate-key -t ssh -f {concourse_config.tsa_host_key_path}"  # noqa: E501
            ],
            state=state,
            host=host,
            sudo=sudo,
        )
    if concourse_config.session_signing_key:
        session_signing_key_file = tempfile.NamedTemporaryFile(delete=False)
        session_signing_key_file.write(
            concourse_config.session_signing_key.encode("utf8")
        )
        files.put(
            name="Write session_signing_host_key file",
            dest=concourse_config.session_signing_key_path,
            user=concourse_config.user,
            mode="600",
            src=session_signing_key_file.name,
            state=state,
            host=host,
            sudo=sudo,
        )
    elif not host.fact.file(concourse_config.session_signing_key_path):
        server.shell(
            name="Generate a session signing key",
            commands=[
                f"{concourse_config.deploy_directory}/bin/concourse generate-key -t rsa -f {concourse_config.session_signing_key_path}"  # noqa: E501
            ],
            state=state,
            host=host,
            sudo=sudo,
        )


@deploy("Manage Worker Node Keys")
def _manage_worker_node_keys(
    concourse_config: ConcourseWorkerConfig, sudo=True, host=None, state=None
):
    if concourse_config.tsa_public_key:
        tsa_key_file = tempfile.NamedTemporaryFile(delete=False)
        tsa_key_file.write(concourse_config.tsa_public_key.encode("utf8"))
        files.put(
            name="Write TSA public key file",
            dest=concourse_config.tsa_public_key_path,
            src=tsa_key_file.name,
            user=concourse_config.user,
            mode="600",
            state=state,
            host=host,
            sudo=sudo,
        )
    if concourse_config.worker_private_key:
        worker_key_file = tempfile.NamedTemporaryFile(delete=False)
        worker_key_file.write(concourse_config.worker_private_key.encode("utf8"))
        files.put(
            name="Write worker private key file",
            dest=concourse_config.worker_private_key_path,
            src=worker_key_file.name,
            user=concourse_config.user,
            mode="600",
            state=state,
            host=host,
            sudo=sudo,
        )
    elif not host.fact.file(concourse_config.worker_private_key_path):
        server.shell(
            name="Generate a worker private key",
            commands=[
                f"{concourse_config.deploy_directory}/bin/concourse generate-key -t ssh -f {concourse_config.worker_private_key_path}"  # noqa: E501
            ],
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
    if concourse_config._node_type == "web":  # noqa: WPS437
        _manage_web_node_keys(concourse_config, state=state, host=host)
    elif concourse_config._node_type == "worker":  # noqa: WPS437
        _manage_worker_node_keys(concourse_config, state=state, host=host)
    return concourse_env_file.changed


@deploy("Register and enable Concourse service")
def register_concourse_service(
    concourse_config: Union[ConcourseWebConfig, ConcourseWorkerConfig],
    state=None,
    host=None,
    restart=False,
):
    # Create Systemd unit to manage Concourse service
    systemd_unit = files.template(
        name="Create concourse Systemd unit definition",
        src=Path(__file__).parent.joinpath("templates/concourse.service.j2"),
        dest="/etc/systemd/system/concourse.service",
        concourse_config=concourse_config,
        state=state,
        host=host,
    )
    # Enable Systemd service and ensure it is running
    systemd.service(
        name="Ensure Concourse service is enabled and running.",
        service="concourse",
        running=True,
        enabled=True,
        restarted=restart,
        daemon_reload=systemd_unit.changed,
        state=state,
        host=host,
    )
