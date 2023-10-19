import os
from pathlib import Path

import yaml
from bridge.lib.magic_numbers import VAULT_HTTP_PORT
from bridge.lib.versions import CONSUL_VERSION, TRAEFIK_VERSION, VAULT_VERSION
from bridge.secrets.sops import set_env_secrets
from pyinfra import host

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
from bilder.components.traefik.models import traefik_file_provider, traefik_static
from bilder.components.traefik.models.component import TraefikConfig
from bilder.components.traefik.steps import (
    configure_traefik,
    install_traefik_binary,
    traefik_service,
)
from bilder.components.vector.models import VectorConfig
from bilder.components.vector.steps import (
    configure_vector,
    install_vector,
    vector_service,
)
from bilder.facts.has_systemd import HasSystemd

VERSIONS = {
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
    "traefik": os.environ.get("TRAEFIK_VERSION", TRAEFIK_VERSION),
}
TEMPLATES_DIRECTORY = Path(__file__).parent.joinpath("templates")
FILES_DIRECTORY = Path(__file__).parent.joinpath("files")
VECTOR_INSTALL_NAME = os.environ.get("VECTOR_LOG_PROXY_NAME", "vector-log-proxy")


# Configure and install traefik
traefik_config = TraefikConfig(
    static_configuration=traefik_static.TraefikStaticConfig.model_validate(
        yaml.safe_load(
            FILES_DIRECTORY.joinpath("traefik", "static_config.yaml").read_text()
        )
    ),
    file_configurations={
        Path("vector.yaml"): traefik_file_provider.TraefikFileConfig.model_validate(
            yaml.safe_load(
                FILES_DIRECTORY.joinpath("traefik", "dynamic_config.yaml").read_text()
            )
        )
    },
)
install_traefik_binary(traefik_config)
traefik_config_changed = configure_traefik(traefik_config)

# Set up configuration objects
set_env_secrets(Path("consul/consul.env"))
consul_configuration = {Path("00-default.json"): ConsulConfig()}
vector_config = VectorConfig(is_proxy=True)
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
        address=f"https://active.vault.service.consul:{VAULT_HTTP_PORT}",
        tls_skip_verify=True,
    ),
    auto_auth=VaultAutoAuthConfig(
        method=VaultAutoAuthMethod(
            type="aws",
            mount_path="auth/aws",
            config=VaultAutoAuthAWS(role="vector-log-proxy"),
        ),
        sink=[VaultAutoAuthSink(type="file", config=[VaultAutoAuthFileSink()])],
    ),
    template=[
        VaultTemplate(
            contents=(
                '{{ with secret "secret-operations/global/odl_wildcard_cert" }}'
                "{{ printf .Data.key }}{{ end }}"
            ),
            destination=Path(f"{vector_config.tls_config_directory}/odl_wildcard.key"),
        ),
        VaultTemplate(
            contents=(
                '{{ with secret "secret-operations/global/odl_wildcard_cert" }}'
                "{{ printf .Data.value }}{{ end }}"
            ),
            destination=Path(f"{vector_config.tls_config_directory}/odl_wildcard.cert"),
        ),
        VaultTemplate(
            contents=(
                '{{ with secret "secret-operations/global/odl_wildcard_cert" }}'
                "{{ printf .Data.key }}{{ end }}"
            ),
            destination=Path(
                f"{traefik_config.configuration_directory}/odl_wildcard.key"
            ),
        ),
        VaultTemplate(
            contents=(
                '{{ with secret "secret-operations/global/odl_wildcard_cert" }}'
                "{{ printf .Data.value }}{{ end }}"
            ),
            destination=Path(
                f"{traefik_config.configuration_directory}/odl_wildcard.cert"
            ),
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


# Install vector
vector_config.configuration_templates[
    TEMPLATES_DIRECTORY.joinpath("vector", "vector-log-proxy.yaml.j2")
] = {}

# The unusual order below is important
# Install vector (will ensure TLS config dir exists)
# Set vault template permissions (will ensure acl tools are installed, sets acl permissions needed by vault user)  # noqa: E501
# Configure vector (will set the acl permissions needed by vector user)
install_vector(vector_config)
vault_template_permissions(vault_config)
configure_vector(vector_config)
vector_service(vector_config)

for product in hashicorp_products:
    configure_hashicorp_product(product)

if host.get_fact(HasSystemd):
    traefik_service(traefik_config=traefik_config, start_immediately=False)
    register_services(hashicorp_products, start_services_immediately=False)
    proxy_consul_dns()
    watched_vector_files = [
        f"{vector_config.tls_config_directory}/odl_wildcard.cert",
        f"{vector_config.tls_config_directory}/odl_wildcard.key",
    ]
    service_configuration_watches(
        service_name="vector", watched_files=watched_vector_files
    )
