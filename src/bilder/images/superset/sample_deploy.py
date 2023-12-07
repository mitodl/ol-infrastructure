from bilder.components.baseline.setup import install_baseline_packages
from bilder.components.superset.models import SupersetConfig
from bilder.components.superset.steps import (
    configure_superset,
    install_superset,
    superset_service,
)
from bilder.facts import has_systemd  # noqa: F401
from pyinfra import host

superset_config = SupersetConfig()
install_baseline_packages()
install_superset(superset_config)
configure_superset(superset_config)
if host.fact.has_systemd:
    superset_service(superset_config)
