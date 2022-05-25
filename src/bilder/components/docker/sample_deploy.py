from pyinfra.operations import server

from bilder.components.docker.docker_compose_steps import deploy_docker_compose
from bilder.components.docker.steps import deploy_docker

deploy_docker()
server.service(
    name="Ensure docker service is running",
    service="docker",
    running=True,
    enabled=True,
)

deploy_docker_compose()
server.service(
    name="Ensure docker compose service is enabled",
    service="docker-compose",
    running=False,
    enabled=True,
)
