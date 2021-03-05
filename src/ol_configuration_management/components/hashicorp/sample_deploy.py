from ol_configuration_management.components.baseline.setup import (
    install_baseline_packages,
)
from ol_configuration_management.components.hashicorp.install import (
    install_hashicorp_products,
)
from ol_configuration_management.components.hashicorp.models import HashicorpConfig

hashicorp_config = HashicorpConfig(
    products=[
        {"name": "consul", "version": "1.9.3"},
        {"name": "consul-template", "version": "0.25.2"},
    ]
)

install_baseline_packages()
install_hashicorp_products(hashicorp_config)
