import os
from io import StringIO
from pathlib import Path

from bridge.lib.magic_numbers import (
    KEYCLOAK_PROMETHEUS_METRICS_PORT,
    VAULT_HTTP_PORT,
)
from bridge.lib.versions import (
    CONSUL_TEMPLATE_VERSION,
    CONSUL_VERSION,
    KEYCLOAK_VERSION,
    VAULT_VERSION,
)
from bridge.secrets.sops import set_env_secrets
from pyinfra import host
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
from bilder.components.hashicorp.consul_template.steps import (
    consul_template_permissions,
)
from bilder.components.hashicorp.steps import (
    configure_hashicorp_product,
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
)
from bilder.components.traefik.models import traefik_static
from bilder.components.traefik.models.component import TraefikConfig
from bilder.components.traefik.steps import configure_traefik
from bilder.components.vector.models import VectorConfig
from bilder.components.vector.steps import install_and_configure_vector
from bilder.facts.has_systemd import HasSystemd
from bilder.lib.linux_helpers import DOCKER_COMPOSE_DIRECTORY
from bilder.lib.template_helpers import (
    CONSUL_TEMPLATE_DIRECTORY,
    place_consul_template_file,
)

set_env_secrets(Path("consul/consul.env"))

TEMPLATES_DIRECTORY = Path(__file__).resolve().parent.joinpath("templates")
FILES_DIRECTORY = Path(__file__).resolve().parent.joinpath("files")

VERSIONS = {
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "consul-template": os.environ.get(
        "CONSUL_TEMPLATE_VERSION", CONSUL_TEMPLATE_VERSION
    ),
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
    "keycloak": os.environ.get("KEYCLOAK_VERSION", KEYCLOAK_VERSION),
}

# Set the keycloak version in defaults
files.put(
    name=f"Set the keycloak version to {VERSIONS['keycloak']}",
    src=StringIO(VERSIONS["keycloak"]),
    dest="/etc/default/keycloak-version",
)

watched_files: list[Path] = []
consul_templates: list[ConsulTemplateTemplate] = []

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
            )
        )
    },
    entry_points={
        "http": traefik_static.EntryPoints(
            address=":80",
            http=traefik_static.Http(
                redirections=traefik_static.Redirections(
                    entry_point=traefik_static.EntryPoint(
                        to="https",
                        scheme="https",
                        permanent=True,
                    )
                )
            ),
        ),
        "https": traefik_static.EntryPoints(address=":443"),
    },
)
traefik_config = TraefikConfig(static_configuration=traefik_static_config)
configure_traefik(traefik_config)

files.put(
    name="Upload docker-compose.yaml",
    src=str(FILES_DIRECTORY.joinpath("docker-compose.yaml")),
    dest=str(DOCKER_COMPOSE_DIRECTORY.joinpath("docker-compose.yaml")),
    mode="0664",
)

files.put(
    name="Upload cache configuration xml",
    src=str(FILES_DIRECTORY.joinpath("cache-ispn-jdbc-ping.xml")),
    dest=str(DOCKER_COMPOSE_DIRECTORY.joinpath("cache-ispn-jdbc-ping.xml")),
    mode="0664",
)

# Install vault, consul, consul-template
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
            config=VaultAutoAuthAWS(role="keycloak-server"),
        ),
        sink=[VaultAutoAuthSink(type="file", config=[VaultAutoAuthFileSink()])],
    ),
    restart_period="5d",
    restart_jitter="12h",
)
vault = Vault(
    versions=VERSIONS["vault"],
    configuration={Path("vault.json"): vault_config},
)

consul_config = {
    Path("00-default.json"): ConsulConfig(
        addresses=ConsulAddresses(dns="127.0.0.1", http="127.0.0.1"),
        advertise_addr='{{ GetInterfaceIP "ens5" }}',
        services=[],
    )
}
consul = Consul(version=VERSIONS["consul"], configuration=consul_config)

# Place consul templates, setup consul-template
dot_env_template = place_consul_template_file(
    name=".env",
    repo_path=FILES_DIRECTORY,
    template_path=Path(CONSUL_TEMPLATE_DIRECTORY),
    destination_path=DOCKER_COMPOSE_DIRECTORY,
)
consul_templates.append(dot_env_template)
watched_files.append(dot_env_template.destination)

consul_template = ConsulTemplate(
    version=VERSIONS["consul-template"],
    configuration={
        Path("00-default.json"): ConsulTemplateConfig(
            vault=ConsulTemplateVaultConfig(),
            template=consul_templates,
            restart_period="7d",
            restart_jitter="12h",
        )
    },
)

hashicorp_products = [vault, consul, consul_template]
for product in hashicorp_products:
    configure_hashicorp_product(product)

# Install and configure vector
vector_config = VectorConfig(
    is_docker=True,
    use_global_log_sink=True,
    use_global_metric_sink=True,
)
vector_config.configuration_templates[
    TEMPLATES_DIRECTORY.joinpath("vector", "keycloak_logs_metrics.yaml")
] = {"keycloak_prometheus_port": KEYCLOAK_PROMETHEUS_METRICS_PORT}
install_and_configure_vector(vector_config)

consul_template_permissions(consul_template.configuration)

if host.get_fact(HasSystemd):
    register_services(hashicorp_products, start_services_immediately=False)
    proxy_consul_dns()
    server.service(
        name="Ensure docker compose service is enabled",
        service="docker-compose",
        running=False,
        enabled=True,
    )

    service_configuration_watches(
        service_name="docker-compose",
        watched_files=watched_files,
    )
