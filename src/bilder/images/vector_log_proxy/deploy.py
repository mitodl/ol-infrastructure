import os
from pathlib import Path

from pyinfra import host

from bilder.components.baseline.steps import service_configuration_watches
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
from bilder.components.vector.models import VectorConfig
from bilder.components.vector.steps import (
    configure_vector,
    install_vector,
    vector_service,
)
from bilder.facts import has_systemd  # noqa: F401
from bridge.lib.magic_numbers import VAULT_HTTP_PORT
from bridge.lib.versions import VAULT_VERSION

VERSIONS = {  # noqa: WPS407
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
}
TEMPLATES_DIRECTORY = Path(__file__).parent.joinpath("templates")
VECTOR_INSTALL_NAME = os.environ.get("VECTOR_LOG_PROXY_NAME", "vector-log-proxy")

# Set up configuration objects
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
        address=f"https://vault.query.consul:{VAULT_HTTP_PORT}",
        tls_skip_verify=True,
    ),
    auto_auth=VaultAutoAuthConfig(
        method=VaultAutoAuthMethod(
            type="aws",
            mount_path=f"auth/aws-aperations",
            config=VaultAutoAuthAWS(role=f"vector-log-proxy"),
        ),
        sink=[VaultAutoAuthSink(type="file", config=[VaultAutoAuthFileSink()])],
    ),
    template=[
        VaultTemplate(
            contents='{{ with secret "secret-operations/global/odl_wildcard_cert" }}'
            "{{ printf .Data.key }}{{ end }}",
            destination=Path(f"{vector_config.tls_config_dir}/odl_wildcard.key"),
        ),
        VaultTemplate(
            contents='{{ with secret "secret-operations/global/odl_wildcard_cert" }}'
            "{{ printf .Data.value }}{{ end }}",
            destination=Path(f"{vector_config.tls_config_dir}/odl_wildcard.cert"),
        ),
    ],
)
vault = Vault(
    version=VERSIONS["vault"],
    configuration={Path("vault.json"): vault_config},
)

hashicorp_products = [vault]
install_hashicorp_products(hashicorp_products)


# Install vector
vector_config.configuration_templates[
    TEMPLATES_DIRECTORY.joinpath("vector", "vector-log-proxy.yaml.j2")
] = {}

# The unusual order below is important
# Install vector (will ensure TLS config dir exists)
# Set vault template permissions (will ensure acl tools are installed, sets acl permissions needed by vault user)
# Configure vector (will set the acl permissions needed by vector user)
install_vector(vector_config)
vault_template_permissions(vault_config)
configure_vector(vector_config)
vector_service(vector_config)

for product in hashicorp_products:
    configure_hashicorp_product(product)

if host.fact.has_systemd:
    register_services(hashicorp_products, start_services_immediately=False)
    watched_vector_files = [
        f"{vector_config.tls_config_dir}/odl_wildcard.cert",
        f"{vector_config.tls_config_dir}/odl_wildcard.key",
    ]
    service_configuration_watches(
        service_name="vector", watched_files=watched_vector_files
    )
