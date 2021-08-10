import os
from pathlib import Path

from pyinfra import host

from bilder.components.baseline.steps import install_baseline_packages
from bilder.components.caddy.models import CaddyConfig, CaddyPlugin
from bilder.components.caddy.steps import caddy_service, configure_caddy, install_caddy
from bilder.components.hashicorp.consul.models import (
    Consul,
    ConsulConfig,
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
from bilder.facts import has_systemd  # noqa: F401

VERSIONS = {  # noqa: WPS407
    "caddy_route53": "v1.1.2",
    "consul": os.environ.get("CONSUL_VERSION", "1.10.0"),
}

install_baseline_packages()
# TODO bootstrap Consul ACL
consul_configuration = {
    Path("00-default.json"): ConsulConfig(
        bootstrap_expect=3,
        server=True,
        telemetry=ConsulTelemetry(),
    )
}

# TODO ACL token
consul_esm_configuration = {
    Path("00-default.json"): ConsulExternalServicesMonitorConfig(token=""),
}


# Install Caddy
caddy_config = CaddyConfig(
    caddyfile=Path(__file__)
    .parent.resolve()
    .joinpath("templates", "consul_caddyfile.j2"),
    plugins=[
        CaddyPlugin(
            repository="github.com/caddy-dns/route53",
            version=VERSIONS["caddy_route53"],
        )
    ],
)
caddy_config.template_context = caddy_config.dict()
install_caddy(caddy_config)
caddy_config_changed = configure_caddy(caddy_config)

# Install Consul and Consul ESM
hashicorp_products = [
    Consul(version=VERSIONS["consul"], configuration=consul_configuration),
    ConsulExternalServicesMonitor(configuration=consul_esm_configuration),
]
install_hashicorp_products(hashicorp_products)
for product in hashicorp_products:
    configure_hashicorp_product(product)

# Manage services
if host.fact.has_systemd:
    caddy_service(caddy_config=caddy_config, do_reload=caddy_config_changed)
    register_services(hashicorp_products, start_services_immediately=False)
    proxy_consul_dns()
