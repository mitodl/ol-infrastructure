from pyinfra import host

from bilder.components.baseline.steps import install_baseline_packages
from bilder.components.caddy.models import CaddyConfig
from bilder.components.caddy.steps import caddy_service, configure_caddy, install_caddy
from bilder.facts.has_systemd import HasSystemd

caddy_config = CaddyConfig(domains=["example.com"])
caddy_config.template_context = caddy_config.model_dump()
install_baseline_packages(packages=["curl", "gnupg"])
install_caddy(caddy_config)
configure_caddy(caddy_config)
if host.get_fact(HasSystemd):
    caddy_service(caddy_config)
