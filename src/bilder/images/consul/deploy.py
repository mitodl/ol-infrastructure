import os
from pathlib import Path

import yaml
from pyinfra import host

from bilder.components.baseline.steps import install_baseline_packages
from bilder.components.hashicorp.consul.models import (
    Consul,
    ConsulConfig,
    ConsulLimitConfig,
    ConsulTelemetry,
)
from bilder.components.hashicorp.consul.steps import proxy_consul_dns
from bilder.components.hashicorp.consul_esm.models import (
    ConsulExternalServicesMonitor,
    ConsulExternalServicesMonitorConfig,
)
from bilder.components.hashicorp.steps import (
    configure_hashicorp_product,
    install_hashicorp_products,
    register_services,
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
from bridge.lib.versions import CONSUL_VERSION, TRAEFIK_VERSION
from bridge.secrets.sops import set_env_secrets

VERSIONS = {
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "traefik": os.environ.get("TRAEFIK_VERSION", TRAEFIK_VERSION),
}
TEMPLATES_DIRECTORY = Path(__file__).parent.joinpath("templates")
FILES_DIRECTORY = Path(__file__).parent.joinpath("files")

install_baseline_packages()
# TODO bootstrap Consul ACL  # noqa: FIX002, TD002, TD004
set_env_secrets(Path("consul/consul.env"))
consul_configuration = {
    Path("00-default.json"): ConsulConfig(
        bootstrap_expect=3,
        server=True,
        ui=True,
        telemetry=ConsulTelemetry(),
        limits=ConsulLimitConfig(http_max_conns_per_client=1000),
    )
}

# TODO ACL token  # noqa: FIX002, TD002, TD004
consul_esm_configuration = {
    Path("00-default.json"): ConsulExternalServicesMonitorConfig(token=""),
}


# Install Traefik
traefik_config = TraefikConfig(
    static_configuration=traefik_static.TraefikStaticConfig.model_validate(
        yaml.safe_load(
            FILES_DIRECTORY.joinpath("traefik", "static_config.yaml").read_text()
        )
    ),
    file_configurations={
        Path("consul.yaml"): traefik_file_provider.TraefikFileConfig.model_validate(
            yaml.safe_load(
                FILES_DIRECTORY.joinpath("traefik", "dynamic_config.yaml").read_text()
            )
        )
    },
)
install_traefik_binary(traefik_config)
traefik_config_changed = configure_traefik(traefik_config)

# Install vector
vector_config = VectorConfig()
vector_config.configuration_templates[
    TEMPLATES_DIRECTORY.joinpath("vector", "consul_logs.yaml")
] = {}
vector_config.configuration_templates[
    TEMPLATES_DIRECTORY.joinpath("vector", "consul_metrics.yaml")
] = {}
install_vector(vector_config)
configure_vector(vector_config)

# Install Consul and Consul ESM
hashicorp_products = [
    Consul(version=VERSIONS["consul"], configuration=consul_configuration),
    ConsulExternalServicesMonitor(configuration=consul_esm_configuration),
]
install_hashicorp_products(hashicorp_products)
for product in hashicorp_products:
    configure_hashicorp_product(product)

# Manage services
if host.get_fact(HasSystemd):
    traefik_service(traefik_config=traefik_config, start_immediately=False)
    register_services(hashicorp_products, start_services_immediately=False)
    proxy_consul_dns()
    vector_service(vector_config)
