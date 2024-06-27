from bilder.components.baseline.steps import install_baseline_packages
from bilder.components.hashicorp import steps as hashicorp_steps
from bilder.components.hashicorp.consul.models import Consul
from bilder.components.hashicorp.consul_template.models import ConsulTemplate
from bilder.components.hashicorp.nomad.models import Nomad
from bilder.components.hashicorp.vault.models import Vault

products = [Consul(systemd_execution_type="exec"), ConsulTemplate(), Vault(), Nomad()]

install_baseline_packages()
hashicorp_steps.install_hashicorp_products(products)
hashicorp_steps.register_services(products)
for product in products:
    hashicorp_steps.configure_hashicorp_product(product)
