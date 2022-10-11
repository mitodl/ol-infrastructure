import io
import os
from pathlib import Path

from pyinfra import host
from pyinfra.operations import files

from bilder.components.hashicorp.consul.models import (
    Consul,
    ConsulAddresses,
    ConsulConfig,
)
from bilder.components.hashicorp.consul.steps import proxy_consul_dns
from bilder.components.hashicorp.consul_template.models import (
    ConsulTemplate,
    ConsulTemplateConfig,
    ConsulTemplateTemplate,
    ConsulTemplateVaultConfig,
)
from bilder.components.hashicorp.consul_template.steps import (
    consul_template_permissions,
)
from bilder.components.hashicorp.steps import (
    configure_hashicorp_product,
    install_hashicorp_products,
)
from bilder.components.hashicorp.vault.models import (
    Vault,
    VaultAgentCache,
    VaultAgentConfig,
    VaultAutoAuthAWS,
    VaultAutoAuthConfig,
    VaultAutoAuthFileSink,
    VaultAutoAuthMethod,
    VaultAutoAuthSink,
    VaultConnectionConfig,
    VaultListener,
    VaultTCPListener,
)
from bilder.components.vector.models import VectorConfig
from bilder.facts.has_systemd import HasSystemd
from bilder.lib.linux_helpers import DOCKER_COMPOSE_DIRECTORY
from bilder.lib.template_helpers import (
    CONSUL_TEMPLATE_DIRECTORY,
    place_consul_template_file,
)
from bridge.lib.magic_numbers import VAULT_HTTP_PORT
from bridge.lib.versions import (
    CONSUL_TEMPLATE_VERSION,
    CONSUL_VERSION,
    OVS_VERSION,
    VAULT_VERSION,
)
from bridge.secrets.sops import set_env_secrets

TEMPLATES_DIRECTORY = Path(__file__).resolve().parent.joinpath("templates")
FILES_DIRECTORY = Path(__file__).resolve().parent.joinpath("files")
VERSIONS = {
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "consul-template": os.environ.get(
        "CONSUL_TEMPLATE_VERSION", CONSUL_TEMPLATE_VERSION
    ),
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
    "ovs": os.environ.get("OVS_VERSION", OVS_VERSION),
}

set_env_secrets(Path("consul/consul.env"))

files.put(
    name="Set the odl-video-service version",
    src=io.StringIO(VERSIONS["ovs"]),
    dest="/etc/default/ovs-version",
)

files.directory(
    name="Create docker compose directory",
    path=str(DOCKER_COMPOSE_DIRECTORY),
    user="root",
    group="root",
    present=True,
)

files.directory(
    name="Create /var/log/odl-video directory",
    path="/var/log/odl-video",
    user="root",
    group="root",
    mode="777",
)

watched_files: list[Path] = []
consul_templates: list[ConsulTemplateTemplate] = []

place_consul_template_file(
    name=".env",
    repo_path=FILES_DIRECTORY,
    template_path=Path(CONSUL_TEMPLATE_DIRECTORY),
    destination_path=Path(DOCKER_COMPOSE_DIRECTORY),
    consul_templates=consul_templates,
    watched_files=watched_files,
    mode="0660",
)

place_consul_template_file(
    name="docker-compose.yaml",
    repo_path=FILES_DIRECTORY,
    template_path=Path(CONSUL_TEMPLATE_DIRECTORY),
    destination_path=Path(DOCKER_COMPOSE_DIRECTORY),
    consul_templates=consul_templates,
    watched_files=watched_files,
    mode="0660",
)

# Install and Configure Consul and Vault
consul_configuration = {
    Path("00-default.json"): ConsulConfig(
        addresses=ConsulAddresses(dns="127.0.0.1", http="127.0.0.1"),
        advertise_addr='{{ GetInterfaceIP "ens5" }}',
        services=[],
    )
}

# Install vault, consul, and consul-template
vault_config = VaultAgentConfig(
    cache=VaultAgentCache(use_auto_auth_token="force"),
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
    auto_auth=VaultAutoAuthConfig(
        method=VaultAutoAuthMethod(
            type="aws",
            mount_path="auth/aws",
            config=VaultAutoAuthAWS(role="ovs-server"),
        ),
        sink=[VaultAutoAuthSink(type="file", config=[VaultAutoAuthFileSink()])],
    ),
)
vault = Vault(
    version=VERSIONS["vault"],
    configuration={Path("vault.json"): vault_config},
)
consul = Consul(version=VERSIONS["consul"], configuration=consul_configuration)

consul_template = ConsulTemplate(
    version=VERSIONS["consul-template"],
    configuration={
        Path("00-default.json"): ConsulTemplateConfig(
            vault=ConsulTemplateVaultConfig(),
            template=consul_templates,
        )
    },
)

vector_config = VectorConfig(is_proxy=False)

# Install consul-template because the docker-baseline-ami doesn't come with it
install_hashicorp_products([consul_template])

hashicorp_products = [vault, consul, consul_template]
for product in hashicorp_products:
    configure_hashicorp_product(product)

consul_template_permissions(consul_template.configuration)

if host.get_fact(HasSystemd):
    # TODO MD 20221011 revisit this, Need to start most of these services by default
    # register_services(hashicorp_products, start_services_immediately=False)
    proxy_consul_dns()
    # server.service(
    #    name="Ensure docker compose service is enabled",
    #    service="docker-compose",
    #    running=False,
    #    enabled=True,
    # )

    watched_docker_compose_files = [
        DOCKER_COMPOSE_DIRECTORY + "/.env",
    ]
    # service_configuration_watches(
    #    service_name="docker-compose", watched_files=watched_docker_compose_files
    # )
