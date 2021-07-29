# TODO: Mount separate disk for Raft data to simplify snapshot backup/restore
# TODO: Create substructure module to populate secrets mounts
import os
from pathlib import Path

from pyinfra import host

from bilder.components.baseline.steps import service_configuration_watches
from bilder.components.caddy.models import CaddyConfig
from bilder.components.caddy.steps import caddy_service, configure_caddy, install_caddy
from bilder.components.hashicorp.consul.models.consul import (
    Consul,
    ConsulConfig,
    ConsulServiceTCPCheck,
)
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
    VaultStorageBackend,
    VaultTCPListener,
)
from bilder.components.hashicorp.vault.steps import vault_template_permissions
from bilder.facts import has_systemd  # noqa: F401
from bridge.lib.magic_numbers import HOURS_IN_MONTH, VAULT_CLUSTER_PORT, VAULT_HTTP_PORT

VERSIONS = {  # noqa: WPS407
    "vault": os.environ.get("VAULT_VERSION", "1.8.0"),
    "consul": os.environ.get("CONSUL_VERSION", "1.10.0"),
}
# Set up configuration objects

# Install Caddy
caddy_config = CaddyConfig(
    caddyfile=Path(__file__)
    .parent.resolve()
    .joinpath("templates", "concourse_caddyfile.j2"),
)
caddy_config.template_context = caddy_config.dict()
install_caddy(caddy_config)
caddy_config_changed = configure_caddy(caddy_config)
if host.fact.has_systemd:
    service_configuration_watches(
        service_name="caddy",
        watched_files=[
            Path("/etc/caddy/odl_wildcard.cert"),
            Path("/etc/caddy/odl_wildcard.key"),
        ],
    )
    caddy_service(caddy_config=caddy_config, do_reload=caddy_config_changed)

# Install Consul agent and Vault server
hours_in_six_months = HOURS_IN_MONTH * 6
vault = Vault(
    configuration=VaultServerConfig(
        listener=[
            VaultListener(
                tcp=VaultTCPListener(
                    address=f"[::]:{VAULT_HTTP_PORT}",
                    cluster_address=f"[::]:{VAULT_CLUSTER_PORT}"
                    # TODO: Provision TLS key and cert during build for HTTPS traffic
                )
            )
        ],
        # Disable swapping to disk because we are using the integrated raft storage
        # backend.
        disable_mlock=True,
        storage=[
            VaultStorageBackend(
                raft=IntegratedRaftStorageBackend(
                    path=Path("/var/lib/vault/raft/"),
                )
            )
        ],
        ui=True,
        service_registration=VaultServiceRegistration(
            consul=ConsulServiceRegistration()
        ),
        plugin_directory=Path("/var/lib/vault/plugins/"),
        max_lease_ttl=f"{hours_in_six_months}h",  # 6 months
        seal=[VaultSealConfig(awskms=VaultAwsKmsSealConfig())],
    )
)
consul_configuration = {
    Path("00-default.json"): ConsulConfig(),
    Path("99-vault.json"): ConsulConfig(
        name="vault",
        port=VAULT_HTTP_PORT,
        check=ConsulServiceTCPCheck(
            name="vault", tcp=f"localhost:{VAULT_HTTP_PORT}", interval="10s"
        ),
    ),
}
consul = Consul(version=VERSIONS["consul"], configuration=consul_configuration)
hashicorp_products = [vault, consul]
install_hashicorp_products(hashicorp_products)
vault_template_permissions(vault.configuration)
for product in hashicorp_products:
    configure_hashicorp_product(product)

# Manage services
if host.fact.has_systemd:
    register_services(hashicorp_products, start_services_immediately=False)
    proxy_consul_dns()
