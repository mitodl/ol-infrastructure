from bilder.components.baseline.setup import install_baseline_packages
from bilder.components.hashicorp.install import install_hashicorp_products
from bilder.components.hashicorp.models import HashicorpConfig

hashicorp_config = HashicorpConfig(
    products=[
        {"name": "consul", "version": "1.9.3"},
        {"name": "consul-template", "version": "0.25.2"},
    ]
)

install_baseline_packages()
install_hashicorp_products(hashicorp_config)
