import io
import os
from pathlib import Path

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
    VaultTemplate,
)
from bilder.components.hashicorp.vault.steps import vault_template_permissions
from bilder.components.pomerium.models import PomeriumConfig
from bilder.components.pomerium.steps import configure_pomerium
from bilder.components.vector.models import VectorConfig
from bilder.components.vector.steps import install_and_configure_vector
from bilder.facts.has_systemd import HasSystemd
from bilder.lib.linux_helpers import DOCKER_COMPOSE_DIRECTORY
from bridge.lib.magic_numbers import DEFAULT_HTTPS_PORT, VAULT_HTTP_PORT
from bridge.lib.versions import (
    AIRBYTE_VERSION,
    CONSUL_TEMPLATE_VERSION,
    CONSUL_VERSION,
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
    "airbyte": os.environ.get("AIRBYTE_VERSION", AIRBYTE_VERSION),
}

set_env_secrets(Path("consul/consul.env"))

pomerium_config = PomeriumConfig(docker_tag="v0.19.1")
configure_pomerium(pomerium_config)

files.put(
    name="Place the airbyte docker-compose.yaml file",
    src=str(FILES_DIRECTORY.joinpath("docker-compose.yaml")),
    dest=str(DOCKER_COMPOSE_DIRECTORY.joinpath("docker-compose.yaml")),
    mode="0664",
)

files.put(
    name="Place Airbyte flags file",
    src=str(FILES_DIRECTORY.joinpath("flags.yml")),
    dest=str(DOCKER_COMPOSE_DIRECTORY.joinpath("flags.yml")),
    mode="0664",
)

files.put(
    name="Set the Airbyte version",
    src=io.StringIO(VERSIONS["airbyte"]),
    dest="/etc/default/airbyte-version",
)

env_template_file = Path("/etc/consul-template/.env.tmpl")
nginx_pomerium_conf_template_file = (
    pomerium_config.configuration_template_directory.joinpath(
        "nginx_pomerium.conf.tmpl"
    )
)
nginx_pomerium_conf_file = Path(
    pomerium_config.configuration_directory.joinpath("nginx_pomerium.conf")
)
nginx_htpasswd_template_file = Path(
    pomerium_config.configuration_template_directory.joinpath("nginx_htpasswd.tmpl")
)
nginx_htpasswd_file = Path(
    pomerium_config.configuration_directory.joinpath("nginx_htpasswd")
)
nginx_proxy_conf_file = Path(
    pomerium_config.configuration_directory.joinpath("nginx_proxy.conf")
)

files.put(
    name="Create the .env template file in docker-compose directory.",
    src=str(TEMPLATES_DIRECTORY.joinpath(".env.tmpl")),
    dest=str(env_template_file),
    mode="0664",
)

files.put(
    name="Create the pomerium nginx configuration file template",
    src=str(FILES_DIRECTORY.joinpath("nginx_pomerium.conf.tmpl")),
    dest=str(nginx_pomerium_conf_template_file),
    mode="0664",
)

files.put(
    name="Create the nginx htpasswd file template for nginx basic auth on the dagster bypass.",  # noqa: E501
    src=str(FILES_DIRECTORY.joinpath("nginx_htpasswd.tmpl")),
    dest=str(nginx_htpasswd_template_file),
    mode="0664",
)

files.put(
    name="Create the the proxy nginx configuration file in the pomerium etc directory.",
    src=str(FILES_DIRECTORY.joinpath("nginx_proxy.conf")),
    dest=str(nginx_proxy_conf_file),
    mode="0664",
)


vault_templates = [
    VaultTemplate(
        source=pomerium_config.configuration_template_file,
        destination=pomerium_config.configuration_file,
    ),
    VaultTemplate(
        source=nginx_pomerium_conf_template_file,
        destination=nginx_pomerium_conf_file,
    ),
    VaultTemplate(
        source=nginx_htpasswd_template_file,
        destination=nginx_htpasswd_file,
    ),
    VaultTemplate(
        contents='{{ with secret "secret-operations/global/odl_wildcard_cert" }}'
        "{{ printf .Data.key }}{{ end }}",
        destination=pomerium_config.certificate_key_file,
    ),
    VaultTemplate(
        contents='{{ with secret "secret-operations/global/odl_wildcard_cert" }}'
        "{{ printf .Data.value }}{{ end }}",
        destination=pomerium_config.certificate_file,
    ),
]
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
    template=vault_templates,
)
vault = Vault(
    version=VERSIONS["vault"],
    configuration={Path("vault.json"): vault_config},
)
consul = Consul(version=VERSIONS["consul"], configuration=consul_configuration)

consul_templates = [
    ConsulTemplateTemplate(
        source=env_template_file,
        destination=str(DOCKER_COMPOSE_DIRECTORY.joinpath(".env")),
    )
]
consul_template = ConsulTemplate(
    version=VERSIONS["consul-template"],
    configuration={
        Path("00-default.json"): ConsulTemplateConfig(
            vault=ConsulTemplateVaultConfig(),
            template=consul_templates,
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
        pomerium_config.certificate_file,
        pomerium_config.certificate_key_file,
        nginx_htpasswd_file,
        nginx_pomerium_conf_file,
        pomerium_config.configuration_file,
    ]
    service_configuration_watches(
        service_name="docker-compose", watched_files=watched_docker_compose_files
    )
