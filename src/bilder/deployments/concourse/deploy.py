from pathlib import Path

from pyinfra import host

from bilder.components.baseline.setup import install_baseline_packages
from bilder.components.caddy.models import CaddyConfig, CaddyPlugin
from bilder.components.caddy.steps import caddy_service, configure_caddy, install_caddy
from bilder.components.concourse.models import (
    ConcourseBaseConfig,
    ConcourseWebConfig,
    ConcourseWorkerConfig,
)
from bilder.components.concourse.steps import (
    configure_concourse,
    install_concourse,
    register_concourse_service,
)
from bilder.components.hashicorp.consul.models.consul import Consul
from bilder.components.hashicorp.consul.models.consul_template import ConsulTemplate
from bilder.components.hashicorp.steps import (
    configure_hashicorp_product,
    install_hashicorp_products,
    register_services,
)
from bilder.components.hashicorp.vault.models import Vault, VaultAgentConfig
from bilder.facts import has_systemd  # noqa: F401

# Install Concourse
concourse_base_config = ConcourseBaseConfig(version="7.1.0")
concourse_config_map = {
    "web": ConcourseWebConfig(
        admin_user="oldevops",
        authorized_worker_keys=[],
        public_domain="cicd.odl.mit.edu",
    ),
    "worker": ConcourseWorkerConfig(),
}
concourse_config = concourse_config_map[host.data.node_type]
install_baseline_packages()
install_changed = install_concourse(concourse_base_config)
config_changed = configure_concourse(concourse_config)

# Install Consul, Vault Agent, and Consul Template
hashicorp_products = [Consul(), ConsulTemplate(), Vault(config=VaultAgentConfig())]
install_hashicorp_products(hashicorp_products)

# Install Caddy
caddy_config = CaddyConfig(
    caddyfile=Path(__file__).parent.joinpath("templates", "concourse_caddyfile.j2"),
    domains=["code-pipelines-qa.odl.mit.edu"],
    plugins=[CaddyPlugin(repository="github.com/caddy-dns/route53", version="v1.1.1")],
)
install_caddy(caddy_config)
caddy_config_changed = configure_caddy(caddy_config)

# Manage services
if host.fact.has_systemd:
    register_concourse_service(
        concourse_config, restart=install_changed or config_changed
    )
    caddy_service(do_reload=caddy_config_changed)
    register_services(hashicorp_products)

for product in hashicorp_products:
    configure_hashicorp_product(product)
