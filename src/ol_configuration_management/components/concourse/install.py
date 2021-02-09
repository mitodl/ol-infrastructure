import httpx
from pyinfra import host
from pyinfra.api import deploy
from pyinfra.operations import files, server


@deploy
def install_concourse_binary():
    # Create a Concourse system user
    concourse_user = server.user(
        name="Create the Concourse system user",
        user="concourse",
        present=True,
        home="/opt/concourse/",
        ensure_home=True,
        shell="/bin/false",
        system=True,
    )

    # Create Concourse directory under /opt to hold application environment
    concourse_app_directory = files.directory(
        name="Create Concourse application directory",
        path="/opt/concourse/",
        present=True,
        user="concourse",
        recursive=True,
    )

    # Download latest Concourse release from GitHub
    concourse_version = "6.7.4"  # host.data.concourse_version
    concourse_archive = f"https://github.com/concourse/concourse/releases/download/v{concourse_version}/concourse-{concourse_version}-linux-amd64.tgz"
    concourse_archive_hash = f"https://github.com/concourse/concourse/releases/download/v{concourse_version}/concourse-{concourse_version}-linux-amd64.tgz.sha1"
    files.download(
        name="Download the Concourse release archive",
        src=concourse_archive,
        dest=f"/tmp/concourse-{concourse_version}.tgz",
        sha1sum=httpx.get(concourse_archive_hash).read().decode("utf8").split()[0],
    )

    # Unpack Concourse to /opt/concourse

    # Create Systemd unit to manage Concourse service

    # Enable Systemd service and ensure it is running
