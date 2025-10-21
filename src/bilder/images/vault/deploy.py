# TODO: Create substructure module to populate secrets mounts  # noqa: E501, FIX002, TD002
import os
from pathlib import Path

import yaml
from pyinfra import host
from pyinfra.operations import files

from bilder.components.baseline.steps import install_baseline_packages
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
)
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
from bridge.lib.magic_numbers import HOURS_IN_MONTH, VAULT_CLUSTER_PORT, VAULT_HTTP_PORT
from bridge.lib.versions import CONSUL_VERSION, TRAEFIK_VERSION, VAULT_VERSION
from bridge.secrets.sops import set_env_secrets

VERSIONS = {
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "traefik": os.environ.get("TRAEFIK_VERSION", TRAEFIK_VERSION),
}
TEMPLATES_DIRECTORY = Path(__file__).parent.joinpath("templates")
FILES_DIRECTORY = Path(__file__).parent.joinpath("files")

install_baseline_packages(packages=["curl", "gnupg", "jq", "cron"])
# Set up configuration objects
set_env_secrets(Path("consul/consul.env"))
# Install Traefik
traefik_config = TraefikConfig(
    static_configuration=traefik_static.TraefikStaticConfig.model_validate(
        yaml.safe_load(
            FILES_DIRECTORY.joinpath("traefik", "static_config.yaml").read_text()
        )
    ),
    file_configurations={
        Path("vault.yaml"): traefik_file_provider.TraefikFileConfig.model_validate(
            yaml.safe_load(
                FILES_DIRECTORY.joinpath("traefik", "dynamic_config.yaml").read_text()
            )
        )
    },
)
install_traefik_binary(traefik_config)
traefik_config_changed = configure_traefik(traefik_config)

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
    path=str(raft_config.path),
    present=True,
    mode="700",
    user=vault.name,
    group=vault.name,
)
for product in hashicorp_products:
    configure_hashicorp_product(product)

# Put down the basic raft_backup.sh
files.put(
    name="Place raft_backup.sh script.",
    src=str(FILES_DIRECTORY.joinpath("raft_backup.sh")),
    dest="/usr/sbin/raft_backup.sh",
    mode="0700",
)

# Install vector
vector_config = VectorConfig()
vector_config.configuration_templates[
    TEMPLATES_DIRECTORY.joinpath("vector", "vault_logs.yaml")
] = {}
install_vector(vector_config)
configure_vector(vector_config)

# Manage services
if host.get_fact(HasSystemd):
    traefik_service(traefik_config=traefik_config, start_immediately=False)
    register_services(hashicorp_products, start_services_immediately=False)
    proxy_consul_dns()
    vector_service(vector_config)
