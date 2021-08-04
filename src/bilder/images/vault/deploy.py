# TODO: Create substructure module to populate secrets mounts
import os
import tempfile
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
    VaultStorageBackend,
    VaultTCPListener,
)
from bilder.facts import has_systemd  # noqa: F401
from bridge.lib.magic_numbers import HOURS_IN_MONTH, VAULT_CLUSTER_PORT, VAULT_HTTP_PORT

VERSIONS = {  # noqa: WPS407
    "vault": os.environ.get("VAULT_VERSION", "1.8.0"),
    "consul": os.environ.get("CONSUL_VERSION", "1.10.1"),
    "caddy_route53": "v1.1.2",
}

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
    .parent.resolve()
    .joinpath("templates", "vault_caddyfile.j2"),
)
caddy_config.template_context = caddy_config.dict()
install_caddy(caddy_config)
caddy_config_changed = configure_caddy(caddy_config)

# Install Consul agent and Vault server
hours_in_six_months = HOURS_IN_MONTH * 6
vault = Vault(
    configuration={
        Path("vault.json"): VaultServerConfig(
            listener=[
                VaultListener(
                    tcp=VaultTCPListener(
                        address=f"[::]:{VAULT_HTTP_PORT}",
                        cluster_address=f"[::]:{VAULT_CLUSTER_PORT}",
                        tls_cert_file=Path("/etc/vault/ssl/vault.cert"),
                        tls_key_file=Path("/etc/vault/ssl/vault.key"),
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
    }
)
consul_configuration = {
    Path("00-default.json"): ConsulConfig(),
}
consul = Consul(version=VERSIONS["consul"], configuration=consul_configuration)
hashicorp_products = [vault, consul]
install_hashicorp_products(hashicorp_products)
for product in hashicorp_products:
    configure_hashicorp_product(product)

# Manage services
if host.fact.has_systemd:
    caddy_service(caddy_config=caddy_config, do_reload=caddy_config_changed)
    register_services(hashicorp_products, start_services_immediately=False)
    proxy_consul_dns()

    # Write to /etc/default/vault
    with tempfile.NamedTemporaryFile(delete=False, mode="w") as vault_env:
        # Automate setting the vault cluster address to use the private IP
        vault_env.write(
            "VAULT_CLUSTER_ADDR=https://"
            r"$(ip -o route get to 8.8.8.8 | sed -n 's/.*src \\([0-9.]\\+\\).*/\1/p')"
            f":{VAULT_CLUSTER_PORT}\n"
        )
        files.put(
            name="Upload Vault environment file",
            src=vault_env.name,
            dest="/etc/default/vault",
        )
