import os
from pathlib import Path

from pyinfra import host

from bilder.components.baseline.steps import service_configuration_watches
from bilder.components.caddy.models import CaddyConfig
from bilder.components.caddy.steps import (
    caddy_service,
    configure_caddy,
    create_placeholder_tls_config,
    install_caddy,
)
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
from bilder.facts.has_systemd import HasSystemd
from bridge.lib.magic_numbers import VAULT_HTTP_PORT
from bridge.lib.versions import CONSUL_VERSION, VAULT_VERSION
from bridge.secrets.sops import set_env_secrets

VERSIONS = {
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
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
            destination=Path("/etc/caddy/odl_wildcard.key"),
        ),
        VaultTemplate(
            contents='{{ with secret "secret-operations/global/odl_wildcard_cert" }}'
            "{{ printf .Data.value }}{{ end }}",
            destination=Path("/etc/caddy/odl_wildcard.cert"),
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

# Install and Configure Caddy and Tika
tika_config = TikaConfig()
install_tika(tika_config)
configure_tika(tika_config)

caddy_config = CaddyConfig(
    caddyfile=Path(__file__).resolve().parent.joinpath("templates", "caddyfile.j2"),
)
caddy_config.template_context = caddy_config.model_dump()
install_caddy(caddy_config)
caddy_config_changed = configure_caddy(caddy_config)

vault_template_permissions(vault_config)
create_placeholder_tls_config(caddy_config)

# Install vector
vector_config.configuration_templates[
    TEMPLATES_DIRECTORY.joinpath("vector", "tika-logs.yaml.j2")
] = {}
vector_config.configuration_templates[
    TEMPLATES_DIRECTORY.joinpath("vector", "caddy-logs.yaml.j2")
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

    watched_caddy_files = [
        "/etc/caddy/odl_wildcard.cert",
        "/etc/caddy/odl_wildcard.key",
    ]
    service_configuration_watches(
        service_name="caddy",
        watched_files=watched_caddy_files,
    )
    caddy_service(caddy_config=caddy_config, do_reload=caddy_config_changed)
