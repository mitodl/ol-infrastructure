import os
from pathlib import Path
from typing import Any, Optional

from pyinfra.api.deploy import deploy
from pyinfra.operations import files

from bridge.lib.versions import DOCKER_COMPOSE_VERSION

DOCKER_COMPOSE_VERSION = os.environ.get(
    "DOCKER_COMPOSE_VERSION", DOCKER_COMPOSE_VERSION
)


def download_docker_compose():
    files.download(
        name="Download the Docker repo file",
        src=f"https://github.com/docker/compose/releases/download/v{DOCKER_COMPOSE_VERSION}/docker-compose-linux-x86_64",
        dest="/usr/local/bin/docker-compose",
        mode="755",
    )
    files.directory(path="/etc/docker/compose/")


def create_systemd_service():
    files.put(
        name="Copy docker compose service file",
        src=str(
            Path(__file__).resolve().parent.joinpath("files", "docker-compose.service")
        ),
        dest="/usr/lib/systemd/system/docker-compose.service",
        mode="755",
    )


@deploy("Deploy Docker Compose")
def deploy_docker_compose(config: Optional[dict[str, Any]] = None):
    download_docker_compose()
    create_systemd_service()
