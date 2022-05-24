from pyinfra.operations import server

from bilder.components.docker.steps import (
    deploy_docker,
)

deploy_docker()
server.service(
    name="Ensure docker service is running",
    service="docker",
    running=True,
    enabled=True,
)