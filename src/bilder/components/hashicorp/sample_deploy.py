from bilder.components.baseline.setup import install_baseline_packages
from bilder.components.hashicorp import steps as hashicorp_steps
from bilder.components.hashicorp.consul.models.consul import Consul
from bilder.components.hashicorp.consul.models.consul_template import ConsulTemplate
from bilder.components.hashicorp.nomad.models import Nomad
from bilder.components.hashicorp.vault.models import Vault

products = [Consul(systemd_execution_type="exec"), ConsulTemplate(), Vault(), Nomad()]

install_baseline_packages()
hashicorp_steps.install_hashicorp_products(products)
hashicorp_steps.register_services(products)
for product in products:
    hashicorp_steps.configure_hashicorp_product(product)
