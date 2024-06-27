from pathlib import Path

from pyinfra import host

from bilder.components.baseline.steps import install_baseline_packages
from bilder.components.traefik.models import traefik_static
from bilder.components.traefik.models.component import TraefikConfig
from bilder.components.traefik.steps import (
    configure_traefik,
    install_traefik_binary,
    traefik_service,
)
from bilder.facts import has_systemd

static_config = traefik_static.TraefikStaticConfig(
    entryPoints={
        # Create the HTTP entrypoint on port 80
        "http": traefik_static.EntryPoints(
            address=":80",
            http=traefik_static.Http(
                redirections=traefik_static.Redirections(
                    entry_point=traefik_static.EntryPoint(
                        to="https",
                        scheme="https",
                        permanent=True,
                    )
                )
            ),
        ),
        "https": traefik_static.EntryPoints(address=":443"),
    },
    certificatesResolvers={
        "letsencrypt_resolver": traefik_static.CertificatesResolvers(
            acme=traefik_static.Acme(
                email="odl-devops@mit.edu",
                storage=Path("/etc/ssl/acme.json"),
                dns_challenge=traefik_static.DnsChallenge(provider="route53"),
            )
        )
    },
    providers=traefik_static.Providers(
        file=traefik_static.FileProvider(filename="/etc/traefik/proxy.yaml")
    ),
)

traefik_config = TraefikConfig(static_configuration=static_config)
install_baseline_packages()
install_traefik_binary(traefik_config)
configure_traefik(traefik_config)
if host.get_fact(has_systemd.HasSystemd):
    traefik_service(traefik_config)
