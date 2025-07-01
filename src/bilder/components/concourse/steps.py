import logging
import tempfile
from pathlib import Path

import httpx
from pyinfra import host
from pyinfra.api import deploy
from pyinfra.facts.files import Directory, File
from pyinfra.operations import files, server, systemd

from bilder.components.concourse.models import (
    ConcourseBaseConfig,
    ConcourseWebConfig,
    ConcourseWorkerConfig,
)


@deploy("Install Concourse")
def install_concourse(concourse_config: ConcourseBaseConfig):
    # Create a Concourse system user
    server.user(  # noqa: S604
        name="Create the Concourse system user",
        user=concourse_config.user,
        present=True,
        home=concourse_config.deploy_directory,
        ensure_home=False,
        shell="/bin/false",
        system=True,
    )
    installation_directory = (
        f"{concourse_config.deploy_directory}-{concourse_config.version}"
    )
    if not host.get_fact(Directory, installation_directory):
        # Download latest Concourse release from GitHub
        concourse_archive = f"https://github.com/concourse/concourse/releases/download/v{concourse_config.version}/concourse-{concourse_config.version}-linux-amd64.tgz"
        concourse_archive_hash = f"{concourse_archive}.sha1"
        concourse_archive_path = f"/tmp/concourse-{concourse_config.version}.tgz"  # noqa: S108
        logging.debug("concourse_archive: %s", concourse_archive)
        logging.debug("concourse_archive_hash: %s", concourse_archive_hash)
        logging.debug("concourse_archive_path: %s", concourse_archive_path)
        files.download(
            name="Download the Concourse release archive",
            src=str(concourse_archive),
            dest=str(concourse_archive_path),
            sha1sum=httpx.get(concourse_archive_hash, follow_redirects=True)
            .read()
            .decode("utf8")
            .split()[0],
        )
        # Unpack Concourse to /opt/concourse
        server.shell(
            name="Extract the Concourse release archive.",
            commands=[
                f"tar -xvzf {concourse_archive_path}",
                f"mv concourse {installation_directory}",
            ],
        )
        # Verify ownership of Concourse directory
        files.directory(
            name="Set ownership of Concourse directory",
            path=str(installation_directory),
            user=concourse_config.user,
        )
    # Link Concourse installation to target directory
    active_installation_path = files.link(
        name="Link Concourse installation to target directory",
        path=str(concourse_config.deploy_directory),
        target=f"{installation_directory}",
        user=concourse_config.user,
        symbolic=True,
        present=True,
    )
    return active_installation_path.changed


@deploy("Manage Web Node Keys")
def _manage_web_node_keys(
    concourse_config: ConcourseWebConfig,
    state=None,
):
    # Create authorized_keys file
    files.template(
        name="Create authorized_keys file to permit worker connections",
        src=str(
            Path(__file__).resolve().parent.joinpath("templates", "authorized_keys.j2")
        ),
        dest=str(concourse_config.authorized_keys_file),
        user=concourse_config.user,
        authorized_keys=concourse_config.authorized_worker_keys or [],
        state=state,
    )
    # Write tsa_host_key and session_signing_key
    if concourse_config.tsa_host_key:
        host_key_file = tempfile.NamedTemporaryFile(delete=False)
        host_key_file.write(concourse_config.tsa_host_key.encode("utf8"))
        files.put(
            name="Write tsa_host_key file",
            dest=str(concourse_config.tsa_host_key_path),
            user=concourse_config.user,
            mode="600",
            src=host_key_file.name,
            state=state,
        )
    elif not host.get_fact(File, str(concourse_config.tsa_host_key_path)):
        server.shell(
            name="Generate a tsa host key",
            commands=[
                f"{concourse_config.deploy_directory}/bin/concourse generate-key -t ssh -f {concourse_config.tsa_host_key_path}"  # noqa: E501
            ],
        )
    if concourse_config.session_signing_key:
        session_signing_key_file = tempfile.NamedTemporaryFile(delete=False)
        session_signing_key_file.write(
            concourse_config.session_signing_key.encode("utf8")
        )
        files.put(
            name="Write session_signing_host_key file",
            dest=str(concourse_config.session_signing_key_path),
            user=concourse_config.user,
            mode="600",
            src=session_signing_key_file.name,
            state=state,
        )
    elif not host.get_fact(File, str(concourse_config.session_signing_key_path)):
        server.shell(
            name="Generate a session signing key",
            commands=[
                f"{concourse_config.deploy_directory}/bin/concourse generate-key -t rsa -f {concourse_config.session_signing_key_path}"  # noqa: E501
            ],
        )


@deploy("Manage Worker Node Keys")
def _manage_worker_node_keys(concourse_config: ConcourseWorkerConfig):
    if concourse_config.tsa_public_key:
        tsa_key_file = tempfile.NamedTemporaryFile(delete=False)
        tsa_key_file.write(concourse_config.tsa_public_key.encode("utf8"))
        files.put(
            name="Write TSA public key file",
            dest=str(concourse_config.tsa_public_key_path),
            src=tsa_key_file.name,
            user=concourse_config.user,
            mode="600",
        )
    if concourse_config.worker_private_key:
        worker_key_file = tempfile.NamedTemporaryFile(delete=False)
        worker_key_file.write(concourse_config.worker_private_key.encode("utf8"))
        files.put(
            name="Write worker private key file",
            dest=str(concourse_config.worker_private_key_path),
            src=worker_key_file.name,
            user=concourse_config.user,
            mode="600",
        )
    elif not host.get_fact(File, str(concourse_config.worker_private_key_path)):
        server.shell(
            name="Generate a worker private key",
            commands=[
                f"{concourse_config.deploy_directory}/bin/concourse generate-key -t ssh -f {concourse_config.worker_private_key_path}"  # noqa: E501
            ],
        )


@deploy("Pull down pre-bundled resource types")
def _install_resource_types(concourse_config: ConcourseWorkerConfig):
    for resource in concourse_config.additional_resource_types or []:
        resource_archive = f"https://{concourse_config.additional_resource_types_s3_location}/{resource}.tgz"
        resource_path = f"/tmp/{resource}"  # noqa: S108
        resource_archive_path = f"{resource_path}/{resource}.tgz"
        addtl_resource_types_dir = concourse_config.additional_resource_types_directory
        server.shell(
            name=f"Setup directory structure for resource_type {resource}",
            commands=[f"mkdir {resource_path}"],
        )
        files.download(
            name=f"Download resource_type {resource}.tgz archive.",
            src=resource_archive,
            dest=resource_archive_path,
        )
        server.shell(
            name=f"Extract the resource_type {resource}.tgz archive.",
            commands=[
                f"tar -xvzf {resource_archive_path} -C {resource_path}",
                f"rm -f {resource_archive_path}",
                f"mv {resource_path} {addtl_resource_types_dir}/",
            ],
        )
        files.directory(
            name=f"Set ownership of resource_type {resource} directory",
            path=f"{concourse_config.additional_resource_types_directory}/{resource}",
            user=concourse_config.user,
        )


@deploy("Configure Concourse")
def configure_concourse(
    concourse_config: ConcourseWebConfig | ConcourseWorkerConfig,
):
    concourse_env_file = files.template(
        name="Create Concourse environment file",
        src=str(Path(__file__).resolve().parent.joinpath("templates/env_file.j2")),
        dest=str(concourse_config.env_file_path),
        concourse_config=concourse_config,
        user=concourse_config.user,
    )
    files.directory(
        name="Create Concourse configuration directory",
        path=str(concourse_config.configuration_directory),
        user=concourse_config.user,
        recursive=True,
        present=True,
    )
    if concourse_config._node_type == "web":  # noqa: SLF001
        _manage_web_node_keys(concourse_config)
    elif concourse_config._node_type == "worker":  # noqa: SLF001
        files.directory(
            name="Create Concourse worker state directory",
            path=str(concourse_config.work_dir),
            present=True,
            user=concourse_config.user,
            recursive=True,
        )
        _manage_worker_node_keys(concourse_config)
        _install_resource_types(concourse_config)
    return concourse_env_file.changed


@deploy("Register and enable Concourse service")
def register_concourse_service(
    concourse_config: ConcourseWebConfig | ConcourseWorkerConfig,
    restart=False,  # noqa: FBT002
):
    # Create Systemd unit to manage Concourse service
    systemd_unit = files.template(
        name="Create concourse Systemd unit definition",
        src=str(
            Path(__file__).resolve().parent.joinpath("templates/concourse.service.j2")
        ),
        dest="/etc/systemd/system/concourse.service",
        concourse_config=concourse_config,
    )
    systemd.service(
        name="Ensure Concourse service is enabled and running.",
        service="concourse",
        running=True,
        enabled=True,
        restarted=restart,
        daemon_reload=systemd_unit.changed,
    )
