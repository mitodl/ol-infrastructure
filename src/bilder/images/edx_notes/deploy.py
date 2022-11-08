from pathlib import Path

from bilder.components.hashicorp.consul_template.models import (
    ConsulTemplateConfig,
    ConsulTemplateTemplate,
)
from bilder.components.traefik.models import traefik_static
from bilder.components.traefik.models.component import TraefikConfig
from bilder.components.traefik.steps import configure_traefik
from bilder.lib.linux_helpers import DOCKER_COMPOSE_DIRECTORY

TEMPLATES_DIRECTORY = Path(__file__).resolve().parent.joinpath("templates")
FILES_DIRECTORY = Path(__file__).resolve().parent.joinpath("files")

traefik_static_config = traefik_static.TraefikStaticConfig(
    log=traefik_static.Log(format="json"),
    providers=[traefik_static.Docker()],
    certificates_resolvers={
        "letsencrypt_resolver": traefik_static.CertificatesResolvers(
            acme=traefik_static.Acme(
                email="odl-devops@mit.edu",
                storage="/etc/traefik/acme.json",
                dns_challenge=traefik_static.DnsChallenge(provider="route53"),
            )
        )
    },
    entry_points={
        "http": traefik_static.EntryPoints(
            address=":80",
            http=traefik_static.Http(
                redirectrions=traefik_static.Redirections(
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
)
traefik_config = TraefikConfig(static_configuration=traefik_static_config)

configure_traefik(traefik_config)

consul_template = ConsulTemplateConfig(
    template=[
        ConsulTemplateTemplate(
            source=FILES_DIRECTORY.joinpath("env.tmpl"),
            destination=DOCKER_COMPOSE_DIRECTORY.joinpath(".env"),
        )
    ]
)
