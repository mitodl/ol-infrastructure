from pyinfra import host

from bilder.components.baseline.steps import install_baseline_packages
from bilder.components.vector.models import VectorConfig
from bilder.components.vector.steps import (
    configure_vector,
    install_vector,
    vector_service,
)
from bilder.facts.has_systemd import HasSystemd

vector_config = VectorConfig()
install_baseline_packages()
install_vector(vector_config)
configure_vector(vector_config)
if host.get_fact(HasSystemd):
    vector_service(vector_config)
