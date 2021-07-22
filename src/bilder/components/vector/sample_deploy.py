from pyinfra import host

from bilder.components.baseline.steps import install_baseline_packages
from bilder.components.vector.models import VectorConfig
from bilder.components.vector.steps import (
    configure_vector,
    install_vector,
    vector_service,
)
from bilder.facts import has_systemd  # noqa: F401

vector_config = VectorConfig()
install_baseline_packages()
install_vector(vector_config)
configure_vector(vector_config)
if host.fact.has_systemd:
    vector_service(vector_config)
