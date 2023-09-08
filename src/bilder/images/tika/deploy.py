import os
from pathlib import Path
from bridge.lib.magic_numbers import VAULT_HTTP_PORT
from bridge.lib.versions import CONSUL_VERSION, VAULT_VERSION, TRAEFIK_VERSION
from bridge.secrets.sops import set_env_secrets
from pyinfra import host
from pyinfra.operations import files

from bilder.components.baseline.steps import service_configuration_watches
from bilder.components.hashicorp.consul.models import Consul, ConsulConfig
from bilder.components.hashicorp.consul.steps import proxy_consul_dns
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
from bilder.components.tika.models import TikaConfig
from bilder.components.tika.steps import configure_tika, install_tika, tika_service
from bilder.components.vector.models import VectorConfig
from bilder.components.vector.steps import (
    configure_vector,
    install_vector,
    vector_service,
)
from bilder.components.traefik.models import traefik_static
from bilder.components.traefik.models.component import TraefikConfig
from bilder.components.traefik.steps import configure_traefik
from bilder.facts.has_systemd import HasSystemd
from bilder.lib.linux_helpers import DOCKER_COMPOSE_DIRECTORY

VERSIONS = {
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
    "traefik": os.environ.get("TRAEFIK_VERSION", TRAEFIK_VERSION),
}
TEMPLATES_DIRECTORY = Path(__file__).parent.joinpath("templates")
VECTOR_INSTALL_NAME = os.environ.get("VECTOR_LOG_PROXY_NAME", "vector-log-proxy")

# Set up configuration objects
set_env_secrets(Path("consul/consul.env"))
consul_configuration = {Path("00-default.json"): ConsulConfig()}
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
            config=VaultAutoAuthAWS(role="tika-server"),
        ),
        sink=[VaultAutoAuthSink(type="file", config=[VaultAutoAuthFileSink()])],
    ),
    template=[
        VaultTemplate(
            contents='{{ with secret "secret-operations/global/odl_wildcard_cert" }}'
            "{{ printf .Data.key }}{{ end }}",
            destination=Path("/etc/traefik/odl_wildcard.key"),
        ),
        VaultTemplate(
            contents='{{ with secret "secret-operations/global/odl_wildcard_cert" }}'
            "{{ printf .Data.value }}{{ end }}",
            destination=Path("/etc/traefik/odl_wildcard.cert"),
        ),
    ],
)
vault = Vault(
    version=VERSIONS["vault"],
    configuration={Path("vault.json"): vault_config},
)
consul = Consul(version=VERSIONS["consul"], configuration=consul_configuration)

hashicorp_products = [vault, consul]
install_hashicorp_products(hashicorp_products)

# Install and Configure Traefik and Tika
tika_config = TikaConfig()
install_tika(tika_config)
configure_tika(tika_config)

# Configure and install traefik
traefik_static_config = traefik_static.TraefikStaticConfig(
    log=traefik_static.Log(format="json"),
    providers=traefik_static.Providers(docker=traefik_static.Docker()),
    certificates_resolvers={
        "letsencrypt_resolver": traefik_static.CertificatesResolvers(
            acme=traefik_static.Acme(
                email="odl-devops@mit.edu",
                storage="/etc/traefik/acme.json",
                dns_challenge=traefik_static.DnsChallenge(provider="route53"),
                caServer="https://acme-v02.api.letsencrypt.org/directory",
            )
        ),
        "letsencrypt_staging_resolver": traefik_static.CertificatesResolvers(
            acme=traefik_static.Acme(
                email="odl-devops@mit.edu",
                storage="/etc/traefik/acme.json",
                dns_challenge=traefik_static.DnsChallenge(provider="route53"),
                caServer="https://acme-staging-v02.api.letsencrypt.org/directory",
            )
        ),
    },
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
    name="Place the airbyte docker-compose.yaml file",
    src=str(TEMPLATES_DIRECTORY.joinpath("docker-compose.yaml")),
    dest=str(DOCKER_COMPOSE_DIRECTORY.joinpath("docker-compose.yaml")),
    mode="0664",
)

vault_template_permissions(vault_config)

# Install vector
vector_config.configuration_templates[
    TEMPLATES_DIRECTORY.joinpath("vector", "tika-logs.yaml.j2")
] = {}
install_vector(vector_config)
configure_vector(vector_config)

# Lay down final configuration for hashicorp products
for product in hashicorp_products:
    configure_hashicorp_product(product)

# Setup systemd daemons for everything
if host.get_fact(HasSystemd):
    tika_service(tika_config)
    vector_service(vector_config)

    register_services(hashicorp_products, start_services_immediately=False)
    proxy_consul_dns()

    watched_docker_compose_files = [
        DOCKER_COMPOSE_DIRECTORY.joinpath(".env"),
        # DOCKER_COMPOSE_DIRECTORY.joinpath(".env_traefik_forward_auth"),
    ]
    service_configuration_watches(
        service_name="docker-compose", watched_files=watched_docker_compose_files
    )
