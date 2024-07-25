import io
import os
from pathlib import Path

from bridge.lib.magic_numbers import DEFAULT_HTTPS_PORT, VAULT_HTTP_PORT
from bridge.lib.versions import (
    AIRBYTE_VERSION,
    CONSUL_TEMPLATE_VERSION,
    CONSUL_VERSION,
    TRAEFIK_VERSION,
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
    ConsulService,
    ConsulServiceTCPCheck,
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
from bilder.components.hashicorp.vault.steps import vault_template_permissions
from bilder.components.traefik.models import traefik_static
from bilder.components.traefik.models.component import TraefikConfig
from bilder.components.traefik.steps import configure_traefik
from bilder.components.vector.models import VectorConfig
from bilder.components.vector.steps import install_and_configure_vector
from bilder.facts.has_systemd import HasSystemd
from bilder.lib.linux_helpers import DOCKER_COMPOSE_DIRECTORY

TEMPLATES_DIRECTORY = Path(__file__).resolve().parent.joinpath("templates")
FILES_DIRECTORY = Path(__file__).resolve().parent.joinpath("files")
VERSIONS = {
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "consul-template": os.environ.get(
        "CONSUL_TEMPLATE_VERSION", CONSUL_TEMPLATE_VERSION
    ),
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
    "airbyte": os.environ.get("AIRBYTE_VERSION", AIRBYTE_VERSION),
    "traefik": os.environ.get("TRAEFIK_VERSION", TRAEFIK_VERSION),
}

set_env_secrets(Path("consul/consul.env"))

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
    servers_transport=traefik_static.ServersTransport(
        forwardingTimeouts=traefik_static.ForwardingTimeouts(idleConnTimeout="300s")
    ),
)
traefik_config = TraefikConfig(
    static_configuration=traefik_static_config, version=VERSIONS["traefik"]
)
traefik_conf_directory = traefik_config.configuration_directory
configure_traefik(traefik_config)

files.put(
    name="Place the airbyte docker-compose.yaml file",
    src=str(FILES_DIRECTORY.joinpath("docker-compose.yaml")),
    dest=str(DOCKER_COMPOSE_DIRECTORY.joinpath("docker-compose.yaml")),
    mode="0664",
)

# Preload some docker images. This will accelerate the first startup
# but prolong the image build.
server.shell(
    name=f"Preload Airbyte containers for version {VERSIONS['airbyte']}",
    commands=["/usr/bin/docker compose pull"],
    _chdir=DOCKER_COMPOSE_DIRECTORY,
    _env={"AIRBYTE_VERSION": VERSIONS["airbyte"]},
)

files.put(
    name="Place Airbyte flags file",
    src=str(FILES_DIRECTORY.joinpath("flags.yml")),
    dest=str(DOCKER_COMPOSE_DIRECTORY.joinpath("flags.yml")),
    mode="0664",
)

files.directory(
    name="Create Temporal dynamic config directory",
    path=str(DOCKER_COMPOSE_DIRECTORY.joinpath("temporal", "dynamicconfig")),
    present=True,
)

files.put(
    name="Place our version of the Temporal dynamicconfig file",
    src=str(FILES_DIRECTORY.joinpath("dynamic_config_development.yaml")),
    dest=str(
        DOCKER_COMPOSE_DIRECTORY.joinpath(
            "temporal", "dynamicconfig", "development.yaml"
        )
    ),
    mode="0664",
)

files.put(
    name="Set the Airbyte version",
    src=io.StringIO(VERSIONS["airbyte"]),
    dest="/etc/default/airbyte-version",
)

certificate_file = traefik_conf_directory.joinpath("star.odl.mit.edu.crt")
certificate_key_file = traefik_conf_directory.joinpath("star.odl.mit.edu.key")
env_template_file = Path("/etc/consul-template/.env.tmpl")
consul_templates_directory = Path("/etc/consul-template")
consul_templates = [
    ConsulTemplateTemplate(
        contents=(
            '{{ with secret "secret-operations/global/odl_wildcard_cert" }}'
            "{{ printf .Data.key }}{{ end }}"
        ),
        destination=Path(certificate_key_file),
        user="root",
        group="root",
    ),
    ConsulTemplateTemplate(
        contents=(
            '{{ with secret "secret-operations/global/odl_wildcard_cert" }}'
            "{{ printf .Data.value }}{{ end }}"
        ),
        destination=Path(certificate_file),
        user="root",
        group="root",
    ),
]

files.put(
    name="Create the .env template file in docker-compose directory.",
    src=str(TEMPLATES_DIRECTORY.joinpath(".env.tmpl")),
    dest=str(env_template_file),
    mode="0664",
)

files.put(
    name="Place the traefik-forward-auth .env file.",
    src=str(TEMPLATES_DIRECTORY.joinpath(".env_traefik_forward_auth.tmpl")),
    dest=str(consul_templates_directory.joinpath(".env_traefik_forward_auth.tmpl")),
    mode="0664",
)

consul_configuration = {
    Path("00-default.json"): ConsulConfig(
        addresses=ConsulAddresses(dns="127.0.0.1", http="127.0.0.1"),
        advertise_addr='{{ GetInterfaceIP "ens5" }}',
        services=[
            ConsulService(
                name="airbyte",
                port=DEFAULT_HTTPS_PORT,
                check=ConsulServiceTCPCheck(
                    name="airbyte-https",
                    tcp="localhost:443",
                    interval="10s",
                ),
            ),
            ConsulService(
                name="airbyte-api",
                port=DEFAULT_HTTPS_PORT,
                check=ConsulServiceTCPCheck(
                    name="airbyte-https",
                    tcp="localhost:8006",
                    interval="10s",
                ),
            ),
        ],
    )
}

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
            config=VaultAutoAuthAWS(role="airbyte-server"),
        ),
        sink=[VaultAutoAuthSink(type="file", config=[VaultAutoAuthFileSink()])],
    ),
    restart_period="5d",
    restart_jitter="12h",
)
vault = Vault(
    version=VERSIONS["vault"],
    configuration={Path("vault.json"): vault_config},
)
consul = Consul(version=VERSIONS["consul"], configuration=consul_configuration)

consul_templates.append(
    ConsulTemplateTemplate(
        source=env_template_file,
        destination=str(DOCKER_COMPOSE_DIRECTORY.joinpath(".env")),
    )
)

consul_templates.append(
    ConsulTemplateTemplate(
        source=str(
            consul_templates_directory.joinpath(".env_traefik_forward_auth.tmpl")
        ),
        destination=str(DOCKER_COMPOSE_DIRECTORY.joinpath(".env_traefik_forward_auth")),
    )
)

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

# Install consul-template because the docker-baseline-ami doesn't come with it
install_hashicorp_products([consul_template])

hashicorp_products = [vault, consul, consul_template]

for product in hashicorp_products:
    configure_hashicorp_product(product)

# Install and configure vector
vector_config = VectorConfig(is_docker=True, use_global_log_sink=True)
vector_config.configuration_templates[
    TEMPLATES_DIRECTORY.joinpath("vector", "airbyte_logs.yaml")
] = {}
install_and_configure_vector(vector_config)

vault_template_permissions(vault_config)
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

    watched_docker_compose_files = [
        DOCKER_COMPOSE_DIRECTORY.joinpath(".env"),
        DOCKER_COMPOSE_DIRECTORY.joinpath(".env_traefik_forward_auth"),
    ]
    service_configuration_watches(
        service_name="docker-compose", watched_files=watched_docker_compose_files
    )
