import os
from io import StringIO
from pathlib import Path

from bridge.lib.magic_numbers import VAULT_HTTP_PORT
from bridge.lib.versions import CONSUL_VERSION, VAULT_VERSION
from bridge.secrets.sops import set_env_secrets
from pyinfra import host
from pyinfra.operations import files, server

from bilder.components.hashicorp.consul.models import (
    Consul,
    ConsulAddresses,
    ConsulConfig,
    ConsulService,
    ConsulServiceTCPCheck,
)
from bilder.components.hashicorp.consul.steps import proxy_consul_dns
from bilder.components.hashicorp.steps import (
    configure_hashicorp_product,
    register_services,
)
from bilder.components.hashicorp.vault.models import (
    Vault,
    VaultAgentCache,
    VaultAgentConfig,
    VaultConnectionConfig,
    VaultListener,
    VaultTCPListener,
)
from bilder.components.vector.models import VectorConfig
from bilder.components.vector.steps import install_and_configure_vector
from bilder.facts.has_systemd import HasSystemd
from bilder.lib.linux_helpers import DOCKER_COMPOSE_DIRECTORY

set_env_secrets(Path("consul/consul.env"))

TEMPLATES_DIRECTORY = Path(__file__).resolve().parent.joinpath("templates")
FILES_DIRECTORY = Path(__file__).resolve().parent.joinpath("files")
VERSIONS = {
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
}

files.put(
    name="Place the codejail docker-compose.yaml file",
    src=str(Path(__file__).resolve().parent.joinpath("files", "docker-compose.yaml")),
    dest=str(DOCKER_COMPOSE_DIRECTORY.joinpath("docker-compose.yaml")),
    mode="0660",
)

OPENEDX_RELEASE = os.environ["OPENEDX_RELEASE"]
files.put(
    name="Create env file for codejail",
    src=StringIO(f"OPENEDX_RELEASE={OPENEDX_RELEASE}"),
    dest=str(DOCKER_COMPOSE_DIRECTORY.joinpath(".env")),
)
# Acceptable values mitxonline, mitx, xpro, mitx-staging
DEPLOYMENT = os.environ["DEPLOYMENT"]
if DEPLOYMENT not in ["mitxonline", "mitx", "xpro", "mitx-staging"]:
    msg = "DEPLOYMENT should be on these values 'mitxonline', 'mitx', 'xpro', 'mitx-staging' "  # noqa: E501
    raise ValueError(msg)

consul_configuration = {
    Path("00-default.json"): ConsulConfig(
        addresses=ConsulAddresses(dns="127.0.0.1", http="127.0.0.1"),
        advertise_addr='{{ GetInterfaceIP "ens5" }}',
    )
}

consul_configuration[Path("01-codejail.json")] = ConsulConfig(
    services=[
        ConsulService(
            name="codejail",
            port=8000,
            check=ConsulServiceTCPCheck(
                name="edxapp-codejail",
                tcp="localhost:8000",
                interval="10s",
            ),
        ),
    ]
)

vault_config = VaultAgentConfig(
    cache=VaultAgentCache(use_auto_auth_token="force"),  # noqa: S106
    listener=[
        VaultListener(
            tcp=VaultTCPListener(
                address=f"127.0.0.1:{VAULT_HTTP_PORT}", tls_disable=True
            )
        )
    ],
    vault=VaultConnectionConfig(
        address=f"https://vault.query.consul:{VAULT_HTTP_PORT}",
        tls_skip_verify=True,
    ),
    restart_period="5d",
    restart_jitter="12h",
)

vault = Vault(
    version=VERSIONS["vault"],
    configuration={Path("vault.json"): vault_config},
)
consul = Consul(version=VERSIONS["consul"], configuration=consul_configuration)

# Install and configure vector
vector_config = VectorConfig(is_docker=True, use_global_log_sink=True)
vector_config.configuration_templates[
    TEMPLATES_DIRECTORY.joinpath("vector", "codejail_logs.yaml")
] = {}
install_and_configure_vector(vector_config)

hashicorp_products = [vault, consul]
for product in hashicorp_products:
    configure_hashicorp_product(product)

if host.get_fact(HasSystemd):
    register_services(hashicorp_products, start_services_immediately=False)
    proxy_consul_dns()
    server.service(
        name="Ensure docker compose service is enabled",
        service="docker-compose",
        running=False,
        enabled=True,
    )
