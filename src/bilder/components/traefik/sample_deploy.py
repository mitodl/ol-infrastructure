from pyinfra import host

from bilder.components.baseline.setup import install_baseline_packages
from bilder.components.traefik.models import TraefikConfig
from bilder.components.traefik.steps import (
    configure_traefik,
    install_traefik,
    traefik_service,
)
from bilder.facts import has_systemd  # noqa: F401

traefik_config = TraefikConfig()
install_baseline_packages()
install_traefik(traefik_config)
configure_traefik(traefik_config)
if host.fact.has_systemd:
    traefik_service(traefik_config)
