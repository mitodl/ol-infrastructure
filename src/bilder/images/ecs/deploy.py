import os
from pathlib import Path

from bridge.lib.versions import CONSUL_VERSION, VAULT_VERSION
from bridge.secrets.sops import set_env_secrets
from pyinfra import host
from pyinfra.operations import apt, server

from bilder.components.docker.steps import create_systemd_service, deploy_docker
from bilder.components.hashicorp.consul.models import (
    Consul,
    ConsulAddresses,
    ConsulConfig,
)
from bilder.components.hashicorp.consul.steps import proxy_consul_dns
from bilder.components.hashicorp.steps import (
    configure_hashicorp_product,
    install_hashicorp_products,
    register_services,
)
from bilder.facts.has_systemd import HasSystemd

VERSIONS = {
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
}

apt.packages(
    name="Remove unattended upgrades",
    packages=["unattended-upgrades"],
    present=False,
)


# Set up configuration objects
set_env_secrets(Path("consul/consul.env"))

consul_configuration = {
    Path("00-default.json"): ConsulConfig(
        addresses=ConsulAddresses(dns="127.0.0.1", http="127.0.0.1"),
        advertise_addr='{{ GetInterfaceIP "ens5" }}',
        services=[],
    )
}

consul = Consul(version=VERSIONS["consul"], configuration=consul_configuration)

hashicorp_products = [consul]
install_hashicorp_products(hashicorp_products)
for product in hashicorp_products:
    configure_hashicorp_product(product)

docker_config = {
    "log-driver": "json-file",
    "log-opts": {"max-size": "10m", "max-file": "3"},
}

deploy_docker(docker_config)

AWS_REGION = "us-east-1"
apt.deb(
    name="Download and install the ecs-init.deb package",
    src=f"https://s3.{AWS_REGION}.amazonaws.com/amazon-ecs-agent-{AWS_REGION}/amazon-ecs-init-latest.amd64.deb",
)

## NOTE
# the userdata of the instantiated server should create the
# file /etc/ecs/ecs.config which looks like : ECS_CLUSTER=<cluster-name>

if host.get_fact(HasSystemd):
    register_services(hashicorp_products, start_services_immediately=False)
    proxy_consul_dns()
    server.service(
        name="Ensure docker service is running",
        service="docker",
        running=True,
        enabled=True,
    )
    server.service(
        name="Ensure the ecs service is enabled",
        service="ecs",
        running=False,
        enabled=True,
    )
    create_systemd_service()
