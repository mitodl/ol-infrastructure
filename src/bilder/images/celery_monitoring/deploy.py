import json
import os
from io import StringIO
from pathlib import Path

from bridge.lib.magic_numbers import VAULT_HTTP_PORT
from bridge.lib.versions import (
    CONSUL_TEMPLATE_VERSION,
    CONSUL_VERSION,
    TRAEFIK_VERSION,
    VAULT_VERSION,
)
from bridge.secrets.sops import set_env_secrets
from pyinfra.context import host
from pyinfra.operations import files, server

from bilder.components.baseline.steps import service_configuration_watches
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
from bilder.components.hashicorp.steps import (
    configure_hashicorp_product,
    install_hashicorp_products,
    register_services,
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
    VaultTemplate,
)
from bilder.components.hashicorp.vault.steps import vault_template_permissions
from bilder.components.traefik.models import traefik_static
from bilder.components.traefik.models.component import TraefikConfig
from bilder.components.traefik.steps import configure_traefik
from bilder.components.vector.models import VectorConfig
from bilder.components.vector.steps import (
    configure_vector,
    install_vector,
    vector_service,
)
from bilder.facts.has_systemd import HasSystemd
from bilder.lib.ami_helpers import build_tags_document
from bilder.lib.linux_helpers import DOCKER_COMPOSE_DIRECTORY
from bilder.lib.template_helpers import (
    CONSUL_TEMPLATE_DIRECTORY,
    place_consul_template_file,
)

VERSIONS = {
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "consul_template": os.environ.get(
        "CONSUL_TEMPLATE_VERSION", CONSUL_TEMPLATE_VERSION
    ),
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
    "traefik": os.environ.get("TRAEFIK_VERSION", TRAEFIK_VERSION),
}
TEMPLATES_DIRECTORY = Path(__file__).parent.joinpath("templates")
FILES_DIRECTORY = Path(__file__).parent.joinpath("files")
VECTOR_INSTALL_NAME = os.environ.get("VECTOR_LOG_PROXY_NAME", "vector-log-proxy")

DOCKER_REPO_NAME = os.environ.get("DOCKER_REPO_NAME", "kodhive/leek")
DOCKER_IMAGE_DIGEST = os.environ.get("DOCKER_IMAGE_DIGEST", "latest")

watched_files: list[Path] = []
consul_templates: list[ConsulTemplateTemplate] = []

# Set up configuration objects
set_env_secrets(Path("consul/consul.env"))
consul_configuration = {
    Path("00-default.json"): ConsulConfig(
        addresses=ConsulAddresses(dns="127.0.0.1", http="127.0.0.1"),
        advertise_addr='{{ GetInterfaceIP "ens5" }}',
    )
}
vector_config = VectorConfig(is_proxy=False)
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
    auto_auth=VaultAutoAuthConfig(
        method=VaultAutoAuthMethod(
            type="aws",
            mount_path="auth/aws",
            config=VaultAutoAuthAWS(role="celery_monitoring"),
        ),
        sink=[VaultAutoAuthSink(type="file", config=[VaultAutoAuthFileSink()])],
    ),
    template=[
        VaultTemplate(
            contents=(
                '{{ with secret "secret-operations/global/odl_wildcard_cert" }}'
                "{{ printf .Data.key }}{{ end }}"
            ),
            destination=Path("/etc/traefik/odl_wildcard.key"),
        ),
        VaultTemplate(
            contents=(
                '{{ with secret "secret-operations/global/odl_wildcard_cert" }}'
                "{{ printf .Data.value }}{{ end }}"
            ),
            destination=Path("/etc/traefik/odl_wildcard.cert"),
        ),
    ],
)
# Configure consul template to add Leek env vars

# Place consul templates, setup consul-template
dot_env_template = place_consul_template_file(
    name=".env",
    repo_path=FILES_DIRECTORY,
    template_path=Path(CONSUL_TEMPLATE_DIRECTORY),
    destination_path=DOCKER_COMPOSE_DIRECTORY,
)
consul_templates.append(dot_env_template)
watched_files.append(dot_env_template.destination)

vault = Vault(
    version=VERSIONS["vault"],
    configuration={Path("vault.json"): vault_config},
)
consul = Consul(version=VERSIONS["consul"], configuration=consul_configuration)
consul_template = ConsulTemplate(
    version=VERSIONS["consul_template"],
    configuration={
        Path("00-default.json"): ConsulTemplateConfig(
            vault=ConsulTemplateVaultConfig(),
            template=consul_templates,
        )
    },
)

hashicorp_products = [vault, consul, consul_template]
install_hashicorp_products(hashicorp_products)

# Configure and install traefik
traefik_static_config = traefik_static.TraefikStaticConfig(
    log=traefik_static.Log(
        level="DEBUG", format="json", filePath="/var/log/traefik_log"
    ),
    providers=traefik_static.Providers(docker=traefik_static.Docker()),
    entry_points={
        "https": traefik_static.EntryPoints(address=":443"),
    },
)
traefik_config = TraefikConfig(
    static_configuration=traefik_static_config, version=VERSIONS["traefik"]
)
traefik_conf_directory = traefik_config.configuration_directory
configure_traefik(traefik_config)

files.put(
    name="Place the leek docker-compose.yaml file",
    src=str(FILES_DIRECTORY.joinpath("docker-compose.yaml")),
    dest=str(DOCKER_COMPOSE_DIRECTORY.joinpath("docker-compose.yaml")),
    mode="0664",
)

vault_template_permissions(vault_config)

# Install vector
vector_config.configuration_templates[
    TEMPLATES_DIRECTORY.joinpath("vector", "leek-logs.yaml.j2")
] = {}
install_vector(vector_config)
configure_vector(vector_config)

# Lay down final configuration for hashicorp products
for product in hashicorp_products:
    configure_hashicorp_product(product)

# Place the tags document
tags_json = json.dumps(
    build_tags_document(
        source_tags={
            "consul_version": VERSIONS["consul"],
            "vault_version": VERSIONS["vault"],
            "traefik_version": VERSIONS["traefik"],
        }
    )
)
files.put(
    name="Place the tags document at /etc/ami_tags.json",
    src=StringIO(tags_json),
    dest="/etc/ami_tags.json",
    mode="0644",
    user="root",
)

# Setup systemd daemons for everything
if host.get_fact(HasSystemd):
    vector_service(vector_config)

    register_services(hashicorp_products, start_services_immediately=False)
    proxy_consul_dns()

    server.service(
        name="Ensure docker compose service is enabled",
        service="docker-compose",
        running=False,
        enabled=True,
    )

    watched_docker_compose_files = [
        DOCKER_COMPOSE_DIRECTORY.joinpath(".env"),
    ]
    service_configuration_watches(
        service_name="docker-compose", watched_files=watched_docker_compose_files
    )
