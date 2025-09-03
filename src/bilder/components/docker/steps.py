import json
import tempfile
from pathlib import Path
from typing import Any

from pyinfra import host
from pyinfra.api.deploy import deploy
from pyinfra.api.exceptions import DeployError
from pyinfra.facts.deb import DebPackages
from pyinfra.facts.server import Command, LinuxDistribution
from pyinfra.operations import apt, files


def _apt_install():
    apt.packages(
        name="Ensure Docker CE prerequisites are present",
        present=True,
        packages=[
            "curl",
            "gnupg-agent",
            "software-properties-common",
        ],
        update=True,
    )

    apt.packages(
        name="Install apt requirements to use HTTPS",
        present=True,
        packages=["apt-transport-https", "ca-certificates"],
        update=True,
        cache_time=3600,
    )
    lsb_release = host.get_fact(LinuxDistribution)
    lsb_id = lsb_release["name"].lower()

    apt.key(
        name="Download the Docker apt key",
        src=f"https://download.docker.com/linux/{lsb_id}/gpg",
    )

    dpkg_arch = host.get_fact(Command, command="dpkg --print-architecture")

    add_apt_repo = apt.repo(
        name="Add the Docker apt repo",
        src=(
            f"deb [arch={dpkg_arch}] https://download.docker.com/linux/{lsb_id}"
            f" {lsb_release['release_meta']['CODENAME']} stable"
        ),
        filename="docker-ce-stable",
    )

    apt.packages(
        name="Install Docker via apt",
        packages=[
            "docker-ce",
            "docker-ce-cli",
            "containerd.io",
            "docker-compose-plugin",
            "amazon-ecr-credential-helper",
        ],
        update=add_apt_repo.changed,
    )


@deploy("Deploy Docker")
def deploy_docker(
    daemon_config: dict[str, Any] | None = None,
    user_config: dict[str, Any] | None = None,
):
    if host.get_fact(DebPackages):
        _apt_install()
    else:
        msg = "Apt not found, pyinfra-docker cannot provision this machine!"
        raise DeployError(
            (msg),
        )

    if daemon_config:
        with tempfile.NamedTemporaryFile("wt", delete=False) as daemon:
            daemon.write(json.dumps(daemon_config, indent=4))
            files.put(
                name="Upload the Docker daemon.json",
                src=daemon.name,
                dest="/etc/docker/daemon.json",
            )

    if user_config:
        with tempfile.NamedTemporaryFile("wt", delete=False) as userconf:
            userconf.write(json.dumps(user_config, indent=4))
            files.put(
                name="Upload the Docker user ~/.docker/config.json",
                src=userconf.name,
                dest="/root/.docker/config.json",
                create_remote_dir=True,
            )


@deploy("Register docker-compose service")
def create_systemd_service():
    files.put(
        name="Copy docker compose service file",
        src=str(
            Path(__file__).resolve().parent.joinpath("files", "docker-compose.service")
        ),
        dest="/usr/lib/systemd/system/docker-compose.service",
        mode="644",
    )
