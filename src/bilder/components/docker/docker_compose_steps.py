from pathlib import Path
from typing import Any, Dict, Optional

from pyinfra.api.deploy import deploy
from pyinfra.operations import files, server


def download_docker_compose():

    server.shell(
        name="Download docker-compose",
        commands=[
            'curl -L "https://github.com/docker/compose/releases/download/1.29.2/docker-compose-linux-x86_64" -o /usr/local/bin/docker-compose'
        ],
    )
    server.shell(
        name="Make Executable",
        commands=["chmod +x /usr/local/bin/docker-compose"],
    )


def create_enable_systemd_service():
    files.template(
        name="Copy docker compose service file",
        src=str(
            Path(__file__).resolve().parent.joinpath("templates/docker-compose.service")
        ),
        dest="/etc/systemd/system/docker-compose.service",
        mode="755",
    )

    server.service(
        name="Enable Docker Compose",
        service="docker-compose ",
        enabled=True,
    )


@deploy("Deploy Docker Compose")
def deploy_docker_compose(config: Optional[Dict[str, Any]] = None):
    download_docker_compose()
    create_enable_systemd_service()
