from pyinfra import host

from bilder.components.baseline.steps import install_baseline_packages
from bilder.components.caddy.models import CaddyConfig
from bilder.components.caddy.steps import caddy_service, configure_caddy, install_caddy
from bilder.facts import has_systemd  # noqa: F401

caddy_config = CaddyConfig(domains=["example.com"])
caddy_config.template_context = caddy_config.dict()
install_baseline_packages(packages=["curl", "gnupg"])
install_caddy(caddy_config)
configure_caddy(caddy_config)
if host.fact.has_systemd:
    caddy_service(caddy_config)
