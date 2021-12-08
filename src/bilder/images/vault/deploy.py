# TODO: Create substructure module to populate secrets mounts
import os
from pathlib import Path

from pyinfra import host
from pyinfra.operations import files

from bilder.components.baseline.steps import install_baseline_packages
from bilder.components.caddy.models import CaddyConfig, CaddyPlugin
from bilder.components.caddy.steps import caddy_service, configure_caddy, install_caddy
from bilder.components.hashicorp.consul.models import Consul, ConsulConfig
from bilder.components.hashicorp.consul.steps import proxy_consul_dns
from bilder.components.hashicorp.steps import (
    configure_hashicorp_product,
    install_hashicorp_products,
    register_services,
)
from bilder.components.hashicorp.vault.models import (
    ConsulServiceRegistration,
    IntegratedRaftStorageBackend,
    Vault,
    VaultAwsKmsSealConfig,
    VaultListener,
    VaultSealConfig,
    VaultServerConfig,
    VaultServiceRegistration,
    VaultTCPListener,
    VaultTelemetryConfig,
    VaultTelemetryListener,
)
from bilder.components.vector.models import VectorConfig
from bilder.components.vector.steps import (
    configure_vector,
    install_vector,
    vector_service,
)
from bilder.facts import has_systemd  # noqa: F401
from bridge.lib.magic_numbers import HOURS_IN_MONTH, VAULT_CLUSTER_PORT, VAULT_HTTP_PORT

VERSIONS = {  # noqa: WPS407
    "vault": os.environ.get("VAULT_VERSION", "1.9.0"),
    "consul": os.environ.get("CONSUL_VERSION", "1.10.3"),
    "caddy_route53": "v1.1.2",
}
TEMPLATES_DIRECTORY = Path(__file__).parent.joinpath("templates")

install_baseline_packages(packages=["curl", "gnupg"])
# Set up configuration objects

# Install Caddy
caddy_config = CaddyConfig(
    plugins=[
        CaddyPlugin(
            repository="github.com/caddy-dns/route53",
            version=VERSIONS["caddy_route53"],
        )
    ],
    caddyfile=Path(__file__)
    .resolve()
    .parent.joinpath("templates", "vault_caddyfile.j2"),
)
caddy_config.template_context = caddy_config.dict()
install_caddy(caddy_config)
caddy_config_changed = configure_caddy(caddy_config)

# Install Consul agent and Vault server
hours_in_six_months = HOURS_IN_MONTH * 6
vault = Vault(
    configuration={
        Path("00-vault.json"): VaultServerConfig(
            api_addr=f"https://active.vault.service.consul:{VAULT_HTTP_PORT}",
            listener=[
                VaultListener(
                    tcp=VaultTCPListener(
                        address=f"[::]:{VAULT_HTTP_PORT}",
                        cluster_address=f"[::]:{VAULT_CLUSTER_PORT}",
                        tls_cert_file=Path("/etc/vault/ssl/vault.cert"),
                        tls_key_file=Path("/etc/vault/ssl/vault.key"),
                    )
                ),
                VaultListener(
                    tcp=VaultTCPListener(
                        address=f"127.0.0.1:{VAULT_HTTP_PORT + 2}",
                        cluster_address=f"127.0.0.1:{VAULT_CLUSTER_PORT + 2}",
                        tls_cert_file=Path("/etc/vault/ssl/vault.cert"),
                        tls_key_file=Path("/etc/vault/ssl/vault.key"),
                        telemetry=VaultTelemetryListener(
                            unauthenticated_metrics_access=True
                        ),
                    )
                ),
            ],
            disable_mlock=True,
            ui=True,
            service_registration=VaultServiceRegistration(
                consul=ConsulServiceRegistration()
            ),
            plugin_directory=Path("/var/lib/vault/plugins/"),
            max_lease_ttl=f"{hours_in_six_months}h",
            seal=[VaultSealConfig(awskms=VaultAwsKmsSealConfig())],
            telemetry=VaultTelemetryConfig(
                disable_hostname=True,
                prometheus_retention_time="5m",
                enable_hostname_label=True,
            ),
        )
    },
    version=VERSIONS["vault"],
)

consul_configuration = {
    Path("00-default.json"): ConsulConfig(),
}
consul = Consul(version=VERSIONS["consul"], configuration=consul_configuration)
hashicorp_products = [vault, consul]
install_hashicorp_products(hashicorp_products)
# Ensure raft config path exists but exact config is loaded via cloud-init
raft_config = IntegratedRaftStorageBackend()
files.directory(
    name="Ensure raft directory exists with proper permissions",
    path=raft_config.path,
    present=True,
    mode="700",
    user=vault.name,
    group=vault.name,
)
for product in hashicorp_products:
    configure_hashicorp_product(product)

# Install vector
vector_config = VectorConfig(
    configuration_templates={
        TEMPLATES_DIRECTORY.joinpath("vector", "vector.yaml"): {},
        TEMPLATES_DIRECTORY.joinpath("vector", "metrics.yaml"): {},
    }
)
install_vector(vector_config)
configure_vector(vector_config)

# Manage services
if host.fact.has_systemd:
    caddy_service(caddy_config=caddy_config, do_reload=caddy_config_changed)
    register_services(hashicorp_products, start_services_immediately=False)
    proxy_consul_dns()
    vector_service(vector_config)
